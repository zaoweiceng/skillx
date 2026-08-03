from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict, deque
from typing import Any

from jsonschema import Draft202012Validator
from pydantic import ValidationError

from app.domain.workflow import CompiledWorkflow, Diagnostic, NodeType, WorkflowDefinition

REFERENCE_PATTERN = re.compile(r"\$\{(?P<root>input|[a-zA-Z][\w-]*)\.(?P<path>[\w.-]+)}")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_json(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode()).hexdigest()


class WorkflowValidationError(ValueError):
    def __init__(self, diagnostics: list[Diagnostic]) -> None:
        self.diagnostics = diagnostics
        super().__init__("workflow validation failed")


class WorkflowCompiler:
    def compile(self, raw_definition: dict[str, Any]) -> CompiledWorkflow:
        diagnostics: list[Diagnostic] = []
        try:
            definition = WorkflowDefinition.model_validate(raw_definition)
        except ValidationError as exc:
            for error in exc.errors():
                path = ".".join(str(item) for item in error["loc"])
                diagnostics.append(
                    Diagnostic(
                        level="error",
                        code="INVALID_DSL",
                        path=path,
                        message=error["msg"],
                    )
                )
            raise WorkflowValidationError(diagnostics) from exc

        self._validate_schema(definition.spec.input_schema, "spec.inputSchema", diagnostics)
        self._validate_schema(definition.spec.output_schema, "spec.outputSchema", diagnostics)

        nodes = {node.id: node for node in definition.spec.nodes}
        incoming: dict[str, list[str]] = defaultdict(list)
        outgoing: dict[str, list[str]] = defaultdict(list)
        for index, edge in enumerate(definition.spec.edges):
            if edge.source not in nodes:
                diagnostics.append(
                    Diagnostic(
                        level="error",
                        code="UNKNOWN_EDGE_SOURCE",
                        path=f"spec.edges.{index}.source",
                        message=f"节点 {edge.source} 不存在",
                    )
                )
                continue
            if edge.target not in nodes:
                diagnostics.append(
                    Diagnostic(
                        level="error",
                        code="UNKNOWN_EDGE_TARGET",
                        path=f"spec.edges.{index}.target",
                        message=f"节点 {edge.target} 不存在",
                    )
                )
                continue
            incoming[edge.target].append(edge.source)
            outgoing[edge.source].append(edge.target)

        order = self._topological_order(nodes, incoming, outgoing, diagnostics)
        self._validate_node_types(definition, diagnostics)
        if len(order) == len(nodes):
            self._validate_references(definition, nodes, incoming, diagnostics)
            self._validate_reachability(nodes, outgoing, diagnostics)

        if any(item.level == "error" for item in diagnostics):
            raise WorkflowValidationError(diagnostics)

        normalized = definition.model_dump(by_alias=True, mode="json")
        return CompiledWorkflow(
            definition=definition,
            topological_order=order,
            dependencies={node_id: incoming[node_id] for node_id in nodes},
            diagnostics=diagnostics,
            digest=digest_json(normalized),
        )

    @staticmethod
    def _validate_schema(schema: dict[str, Any], path: str, diagnostics: list[Diagnostic]) -> None:
        try:
            Draft202012Validator.check_schema(schema)
        except Exception as exc:
            diagnostics.append(
                Diagnostic(
                    level="error",
                    code="INVALID_JSON_SCHEMA",
                    path=path,
                    message=str(exc),
                )
            )

    @staticmethod
    def _topological_order(
        nodes: dict[str, Any],
        incoming: dict[str, list[str]],
        outgoing: dict[str, list[str]],
        diagnostics: list[Diagnostic],
    ) -> list[str]:
        indegree = {node_id: len(incoming[node_id]) for node_id in nodes}
        queue = deque(sorted(node_id for node_id, degree in indegree.items() if degree == 0))
        order: list[str] = []
        while queue:
            node_id = queue.popleft()
            order.append(node_id)
            for target in outgoing[node_id]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    queue.append(target)
        if len(order) != len(nodes):
            diagnostics.append(
                Diagnostic(
                    level="error",
                    code="CYCLIC_GRAPH",
                    path="spec.edges",
                    message="工作流主体必须是有向无环图",
                )
            )
        return order

    @staticmethod
    def _validate_node_types(definition: WorkflowDefinition, diagnostics: list[Diagnostic]) -> None:
        inputs = [node for node in definition.spec.nodes if node.type == NodeType.INPUT]
        outputs = [node for node in definition.spec.nodes if node.type == NodeType.OUTPUT]
        if len(inputs) != 1:
            diagnostics.append(
                Diagnostic(
                    level="error",
                    code="INPUT_NODE_COUNT",
                    path="spec.nodes",
                    message="工作流必须且只能包含一个 Input 节点",
                )
            )
        if len(outputs) != 1:
            diagnostics.append(
                Diagnostic(
                    level="error",
                    code="OUTPUT_NODE_COUNT",
                    path="spec.nodes",
                    message="Local Demo 必须且只能包含一个 Output 节点",
                )
            )
        for index, node in enumerate(definition.spec.nodes):
            if node.side_effect and node.retry.max_attempts > 1 and not node.idempotent:
                diagnostics.append(
                    Diagnostic(
                        level="error",
                        code="UNSAFE_SIDE_EFFECT_RETRY",
                        path=f"spec.nodes.{index}.retry",
                        message="非幂等副作用节点不得自动重试",
                    )
                )
            if node.type == NodeType.SKILLX:
                if node.config.get("package"):
                    diagnostics.append(
                        Diagnostic(
                            level="error",
                            code="SKILLX_PACKAGE_PATH_FORBIDDEN",
                            path=f"spec.nodes.{index}.config.package",
                            message="Workflow 不得携带 SkillX 宿主机路径",
                        )
                    )
                if not node.config.get("interface") or not node.config.get("capabilityVersionId"):
                    diagnostics.append(
                        Diagnostic(
                            level="error",
                            code="SKILLX_VERSION_REQUIRED",
                            path=f"spec.nodes.{index}.config",
                            message="SkillX 节点必须锁定 capabilityVersionId 和 interface",
                        )
                    )
            if node.type == NodeType.MCP and not node.config.get("mcpToolSnapshotId"):
                diagnostics.append(
                    Diagnostic(
                        level="error",
                        code="MCP_TOOL_SNAPSHOT_REQUIRED",
                        path=f"spec.nodes.{index}.config",
                        message="MCP 节点必须锁定 mcpToolSnapshotId",
                    )
                )
            if node.type == NodeType.LLM and not isinstance(node.config.get("outputSchema"), dict):
                diagnostics.append(
                    Diagnostic(
                        level="error",
                        code="LLM_OUTPUT_SCHEMA_REQUIRED",
                        path=f"spec.nodes.{index}.config.outputSchema",
                        message="LLM 节点必须声明结构化输出 Schema",
                    )
                )
            if node.type == NodeType.AGENT:
                if not isinstance(node.config.get("outputSchema"), dict):
                    diagnostics.append(
                        Diagnostic(
                            level="error",
                            code="AGENT_OUTPUT_SCHEMA_REQUIRED",
                            path=f"spec.nodes.{index}.config.outputSchema",
                            message="Agent 节点必须声明结构化输出 Schema",
                        )
                    )
                if not node.config.get("maxSteps") or not isinstance(
                    node.config.get("allowedCapabilities"), list
                ):
                    diagnostics.append(
                        Diagnostic(
                            level="error",
                            code="AGENT_LIMITS_REQUIRED",
                            path=f"spec.nodes.{index}.config",
                            message="Agent 节点必须声明 maxSteps 和 allowedCapabilities",
                        )
                    )

    @staticmethod
    def _validate_references(
        definition: WorkflowDefinition,
        nodes: dict[str, Any],
        incoming: dict[str, list[str]],
        diagnostics: list[Diagnostic],
    ) -> None:
        ancestors: dict[str, set[str]] = {}

        def collect(node_id: str) -> set[str]:
            if node_id in ancestors:
                return ancestors[node_id]
            result: set[str] = set()
            for parent in incoming[node_id]:
                result.add(parent)
                result.update(collect(parent))
            ancestors[node_id] = result
            return result

        for index, node in enumerate(definition.spec.nodes):
            payload = canonical_json(node.input)
            for match in REFERENCE_PATTERN.finditer(payload):
                root = match.group("root")
                if root == "input":
                    continue
                if root not in nodes:
                    diagnostics.append(
                        Diagnostic(
                            level="error",
                            code="UNKNOWN_REFERENCE",
                            path=f"spec.nodes.{index}.input",
                            message=f"引用的节点 {root} 不存在",
                        )
                    )
                elif root not in collect(node.id):
                    diagnostics.append(
                        Diagnostic(
                            level="error",
                            code="UNREACHABLE_REFERENCE",
                            path=f"spec.nodes.{index}.input",
                            message=f"只能引用可达上游节点，{root} 不是 {node.id} 的上游",
                        )
                    )

    @staticmethod
    def _validate_reachability(
        nodes: dict[str, Any],
        outgoing: dict[str, list[str]],
        diagnostics: list[Diagnostic],
    ) -> None:
        input_ids = [node.id for node in nodes.values() if node.type == NodeType.INPUT]
        if len(input_ids) != 1:
            return
        visited: set[str] = set()
        queue = deque(input_ids)
        while queue:
            node_id = queue.popleft()
            if node_id in visited:
                continue
            visited.add(node_id)
            queue.extend(outgoing[node_id])
        for node_id in nodes.keys() - visited:
            diagnostics.append(
                Diagnostic(
                    level="error",
                    code="UNREACHABLE_NODE",
                    path=f"spec.nodes.{node_id}",
                    message=f"节点 {node_id} 无法从 Input 到达",
                )
            )
