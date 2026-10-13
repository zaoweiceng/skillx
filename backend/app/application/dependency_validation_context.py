"""Load pinned dependency test contracts only for workflow example construction.

Implementation source never enters the returned context. The package digest is
checked before reading the immutable version's test data; the current version of
a same-named capability is deliberately irrelevant.
"""
from copy import deepcopy
import json

from app.application.capabilities import resolve_package_path
from app.infrastructure.database import CapabilityVersion, WorkflowVersion
from app.runtime.skillx import package_digest

EXAMPLE_FIELDS = {"input", "expected", "setup", "verify", "httpFixtures", "expectedError", "failureOutput"}


def _examples(raw, interface=None, default_interface=None, *, failures=False):
    if not isinstance(raw, list):
        raise ValueError("依赖 tests.json 必须为样例数组")
    selected = []
    for example in raw:
        if not isinstance(example, dict):
            raise ValueError("依赖验证样例必须为对象")
        if interface and example.get("interface", default_interface) != interface:
            continue
        if bool(example.get("expectedError") or example.get("failureOutput")) != failures:
            continue
        if "input" not in example or "expected" not in example:
            raise ValueError("依赖验证样例缺少 input/expected")
        selected.append({key: deepcopy(value) for key, value in example.items() if key in EXAMPLE_FIELDS})
    return selected


async def load_dependency_validation_context(session, definition, *, supplied_contracts=()):
    """Return each referenced version/interface once, retaining all successful cases.

    ``supplied_contracts`` is the union of suppliedSkillXContracts and
    suppliedWorkflowContracts for this extraction (with their original examples).
    Call this only after graph construction, immediately before WorkflowExamples.
    No examples are executed and no service or model is called here.
    """
    supplied = {item["name"]: item for item in supplied_contracts}
    result = {}

    async def visit(graph, prefix="", ancestors=()):
        if len(ancestors) > 8:
            raise ValueError("验证依赖超过工作流嵌套限制")
        for node in graph.get("spec", {}).get("nodes", []):
            config = node.get("config", {})
            path = prefix + node["id"]
            generated = config.get("generatedSkill") or config.get("generatedWorkflow")
            if generated:
                contract = supplied.get(generated)
                if contract is None:
                    raise ValueError("缺少生成依赖的验证契约：" + generated)
                key = ("generated", generated)
                if key not in result:
                    result[key] = {"name":generated, "nodePaths":[], "examples":_examples(contract.get("examples", [])),
                        "failureExamples":_examples(contract.get("examples", []), failures=True),
                        "executionProfile":contract.get("executionProfile"),
                        "requiresLocalValidation":contract.get("requiresLocalValidation", contract.get("executionProfile") == "local-command")}
                result[key]["nodePaths"].append(path)
                continue
            if node.get("type") not in {"skillx", "capability_call", "workflow_call"}:
                continue
            version_id = config.get("capabilityVersionId") or config.get("workflowVersionId")
            if not version_id:
                raise ValueError("验证依赖缺少锁定版本：" + path)
            skill = await session.get(CapabilityVersion, version_id)
            if skill is None:
                workflow = await session.get(WorkflowVersion, version_id)
                if workflow is None:
                    raise ValueError("验证依赖版本不存在：" + version_id)
                if version_id in ancestors:
                    raise ValueError("验证依赖工作流循环引用")
                if config.get("digest") != workflow.digest:
                    raise ValueError("工作流验证依赖 digest 不匹配")
                await visit(workflow.definition_json, path + "/", (*ancestors, version_id))
                continue
            if skill.status != "published":
                raise ValueError("验证依赖必须是已发布版本")
            if node["type"] == "capability_call" and config.get("digest") != skill.digest:
                raise ValueError("能力验证依赖 digest 不匹配")
            interface = config.get("interface", "execute")
            key = (skill.id, interface)
            if key not in result:
                package = resolve_package_path(skill.package_path)
                if package_digest(package) != skill.digest:
                    raise ValueError("依赖验证包 digest 漂移")
                manifest = skill.manifest_json.get("manifest", {})
                spec = manifest.get("spec", {})
                raw_interface = spec.get("interfaces", {}).get(interface)
                if raw_interface is None:
                    raise ValueError("验证依赖接口不存在：" + interface)
                tests = package / "tests.json"
                raw_examples = json.loads(tests.read_text()) if tests.is_file() else []
                examples = _examples(raw_examples, interface, spec.get("defaultInterface", "execute"))
                failure_examples = _examples(raw_examples, interface, spec.get("defaultInterface", "execute"), failures=True)
                contract = skill.manifest_json.get("interfaces", {}).get(interface, {})
                result[key] = {"inputSchema":deepcopy(contract.get("inputSchema", {})),
                    "outputSchema":deepcopy(contract.get("outputSchema", {})), "versionId":skill.id, "digest":skill.digest, "interface":interface,
                    "name":manifest.get("metadata", {}).get("name", ""), "nodePaths":[],
                    "executionProfile":raw_interface.get("x-executionProfile"),
                    "requiresLocalValidation":raw_interface.get("x-executionProfile") == "local-command",
                    "examples":examples, "failureExamples":failure_examples, "validationContractAvailable":bool(examples)}
            result[key]["nodePaths"].append(path)

    await visit(definition)
    return {"dependencies":list(result.values()), "fixtureContract":(
        "这里只包含图中实际锁定依赖的验证样例，不含实现。examples为成功，failureExamples为结构化失败或预期异常。"
        "前置读取失败不是查无记录；组合流程不得据此执行写入。复用其真实请求结构与序列化，不能猜测HTTP body。"
        "每个样例的setup/input/expected/verify/httpFixtures为完整配套；${fixture.key}在对应setup运行后解析，"
        "setup写入整个对象时引用也保持对象，不能字符串化。组合时适配共享目录和业务输入，按实际分支串接请求队列；"
        "保留原验证语义，不把多个独立setup机械拼接重置中间状态。缺样例时不能伪造已知请求协议。")}
