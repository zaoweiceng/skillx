"""Explicit reuse decisions shared by all three extraction artifact types."""

import ast

from pydantic import BaseModel, ConfigDict, Field


class ReuseReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    comparedIds: list[str] = Field(
        default_factory=list,
        max_length=16,
        description="已比较的最相关能力版本 ID 或 Skill ID；按功能、输入输出和副作用比较，不按名称",
    )
    gap: str = Field(
        pattern=r"\S", description="现有能力无法覆盖的具体增量；改名、换描述或样例不算增量"
    )
    dependencyIds: list[str] = Field(
        default_factory=list,
        max_length=8,
        description="增量必须实际复用的已发布能力版本 ID 或 Skill ID；只实现缺失部分",
    )


def validate_reuse_plan(plan, context, *, required=False):
    catalog = {item["id"]: item for item in context.get("catalog", {}).get("capabilities", [])}
    skills = {item["id"]: item for item in context.get("existingSkills", [])}
    available = catalog | skills
    workflows = [*plan.workflows, *([plan.workflow] if plan.workflow else [])]
    for group, fragments in (
        ("skills", plan.skills),
        ("skillx", plan.skillx),
        ("workflows", workflows),
    ):
        for fragment in fragments:
            target = fragment.reuseSkillId if group == "skills" else fragment.reuseVersionId
            if (group == "skills" and fragment.reuseVersionId) or (
                group != "skills" and fragment.reuseSkillId
            ):
                raise ValueError("指南用 reuseSkillId，可执行能力用 reuseVersionId")
            if target:
                if target not in (skills if group == "skills" else catalog):
                    raise ValueError("复用目标必须存在于当前可用目录，不能编造 ID 或复用已下线产物")
                if fragment.reuseReview and fragment.reuseReview.dependencyIds:
                    raise ValueError("直接复用不能同时要求新增依赖封装")
                if group != "skills" and fragment.requiredEffects:
                    interfaces = catalog[target].get("interfaces", {}).values()
                    if interfaces and all(
                        interface.get("executionProfile") == "pure-python"
                        and not interface.get("effects") and not interface.get("hasDependencies")
                        for interface in interfaces
                    ):
                        raise ValueError("requiredEffects 不能直接复用无依赖的 pure-python formatter；目标必须实际完成外部动作")
                continue
            review = fragment.reuseReview
            if required and available and review is None:
                raise ValueError("新增产物前必须提供 reuseReview：比较已有能力并说明具体功能缺口")
            if not review:
                continue
            if not set(review.comparedIds + review.dependencyIds) <= available.keys():
                raise ValueError("复用评估引用了目录之外的 ID")
            if available and not review.comparedIds:
                raise ValueError("目录非空时必须比较最相关的已有能力，不能跳过检索直接新建")
            if not set(review.dependencyIds) <= set(review.comparedIds):
                raise ValueError("增量依赖必须来自已比较的候选")
            if group != "skills" and not set(review.dependencyIds) <= catalog.keys():
                raise ValueError("可执行能力不能把操作指南当成可调用依赖")


def validate_reuse_implementation(fragment, artifact, group, context):
    """A declared extension must depend on its base instead of copying its implementation."""
    review = fragment.reuseReview
    if not review or not review.dependencyIds:
        return
    catalog = {item["id"]: item for item in context.get("catalog", {}).get("capabilities", [])}
    if group == "workflows":
        referenced = {
            node.get("config", {}).get(key)
            for node in artifact["spec"]["nodes"]
            if node["type"] in {"skillx", "capability_call", "workflow_call"}
            for key in ("capabilityVersionId", "workflowVersionId")
        }
        if not set(review.dependencyIds) <= referenced:
            raise ValueError(
                "增量工作流必须实际调用 reuseReview.dependencyIds 中的锁定版本，禁止复制重造"
            )
    elif group == "skillx":
        called = {
            node.args[0].value
            for node in ast.walk(ast.parse(artifact.source))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and ast.unparse(node.func) == "context.capabilities.call"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        }
        refs = {
            item.get("ref")
            for alias, item in artifact.dependencies.items()
            if alias in called and isinstance(item, dict) and item.get("kind") == "skillx"
        }
        for id in review.dependencyIds:
            item = catalog[id]
            if not any(
                f"{item['name']}@{item['version']}#{interface}" == ref
                for interface in item["interfaces"]
                for ref in refs
            ):
                raise ValueError("增量 SkillX 必须声明精确版本依赖；跨工作流组合请提取为 Workflow")
    else:
        candidates = catalog | {item["id"]: item for item in context.get("existingSkills", [])}
        if any(candidates[id]["name"] not in artifact.content for id in review.dependencyIds):
            raise ValueError("增量 Skill 必须在正文中引用所依赖的已有能力名称和使用方式")


def bind_reuse_schema(schema, context):
    """Expose finite valid IDs and the mandatory decision in the model contract."""
    from copy import deepcopy

    schema = deepcopy(schema)
    # The planner must explicitly decide every artifact group. Pydantic defaults
    # are useful for legacy persisted plans, but accepting an omitted workflows
    # field silently discarded a composition promised in the model's reason.
    schema["required"] = list(dict.fromkeys([
        *schema.get("required", []), "skills", "skillx", "workflows",
    ]))
    versions = [item["id"] for item in context.get("catalog", {}).get("capabilities", [])]
    skills = [item["id"] for item in context.get("existingSkills", [])]
    for name in ("ReusableFragment", "WorkflowFragment"):
        definition = schema.get("$defs", {}).get(name)
        if not definition:
            continue
        definition["required"] = list(dict.fromkeys([*definition.get("required", []), "requiredEffects"]))
        properties = definition["properties"]
        properties["reuseVersionId"] = {"enum": [None, *versions]}
        properties["reuseSkillId"] = {
            "enum": [None, *skills] if name != "WorkflowFragment" else [None]
        }
        if versions or skills:
            choices = [
                {"required": ["reuseReview"], "properties": {"reuseReview": {"type": "object"}}}
            ]
            for field, ids in (
                ("reuseVersionId", versions),
                ("reuseSkillId", skills if name != "WorkflowFragment" else []),
            ):
                if ids:
                    choices.append({"required": [field], "properties": {field: {"enum": ids}}})
            definition.setdefault("allOf", []).append({"anyOf": choices})
    review = schema.get("$defs", {}).get("ReuseReview")
    if review:
        for field in ("comparedIds", "dependencyIds"):
            review["properties"][field]["items"] = (
                {"enum": [*versions, *skills]} if versions or skills else False
            )
        if versions or skills:
            review["properties"]["comparedIds"]["minItems"] = 1
            review.setdefault("required", []).append("comparedIds")
    return schema
