from __future__ import annotations

from typing import Any, Literal

from jsonschema import Draft202012Validator

TERMINAL_STATES = {
    "succeeded",
    "failed",
    "skipped",
    "cancelled",
    "lease_expired",
}

Readiness = Literal["waiting", "ready", "skipped"]


def fallback_only_targets(nodes: dict[str, Any]) -> set[str]:
    return {
        node.fallback.target
        for node in nodes.values()
        if node.fallback is not None
    }


def edge_ready(
    source: str,
    statuses: dict[str, str],
    substitutions: dict[str, Any],
) -> bool:
    status = statuses.get(source)
    if status is None:
        return False
    if source in substitutions and status == "failed":
        return True
    return status in TERMINAL_STATES


def edge_selected(
    source: str,
    when: bool | None,
    statuses: dict[str, str],
    outputs: dict[str, Any],
    on_errors: dict[str, str],
    substitutions: dict[str, Any],
) -> bool:
    status = statuses[source]
    substituted = source in substitutions and status == "failed"
    if not (
        status == "succeeded"
        or substituted
        or (status == "failed" and on_errors.get(source) == "continue")
    ):
        return False
    if when is None:
        return True
    output = substitutions[source] if substituted else outputs.get(source, {})
    return bool((output or {}).get("result")) == when


def node_readiness(
    incoming: list[Any],
    statuses: dict[str, str],
    outputs: dict[str, Any],
    on_errors: dict[str, str],
    substitutions: dict[str, Any] | None = None,
) -> tuple[Readiness, list[dict[str, Any]]]:
    substitutions = substitutions or {}
    if incoming and not all(
        edge_ready(edge.source, statuses, substitutions) for edge in incoming
    ):
        return "waiting", []
    selected = [
        {
            "source": edge.source,
            "target": edge.target,
            **({"when": edge.when} if edge.when is not None else {}),
        }
        for edge in incoming
        if edge_selected(
            edge.source,
            edge.when,
            statuses,
            outputs,
            on_errors,
            substitutions,
        )
    ]
    if incoming and not selected:
        return "skipped", []
    return "ready", selected


def output_schema_errors(schema: dict[str, Any], output: Any) -> list[str]:
    return [
        error.message
        for error in Draft202012Validator(schema).iter_errors(output)
    ]
