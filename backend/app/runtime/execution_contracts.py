"""Execution contracts shared by all node implementations and the scheduler."""

import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.domain.workflow import NodeResult, NodeType, WorkflowNode


class ExecutionCancelledError(Exception):
    pass


@dataclass
class CancellationToken:
    event: asyncio.Event

    @property
    def cancelled(self) -> bool:
        return self.event.is_set()

    def check(self) -> None:
        if self.cancelled:
            raise ExecutionCancelledError("执行已取消")


@dataclass
class RunContext:
    run_id: str
    run_input: dict[str, Any]
    node_run_id: str
    cancellation: CancellationToken
    runtime_snapshot: dict[str, Any] = field(default_factory=dict)


class TaskExecutor(Protocol):
    node_type: NodeType

    async def execute(
        self, context: RunContext, node: WorkflowNode, inputs: dict[str, Any]
    ) -> NodeResult: ...


NodeExecutor = TaskExecutor
