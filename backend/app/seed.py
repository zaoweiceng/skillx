from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from app.application.compiler import WorkflowCompiler
from app.application.mcp import discover_tools, register_server
from app.infrastructure.database import (
    Component,
    SessionLocal,
    Workflow,
    WorkflowVersion,
)


async def seed() -> str:
    example = Path(__file__).resolve().parents[2] / "examples" / "greeting-workflow.json"
    definition = json.loads(example.read_text(encoding="utf-8"))
    compiled = WorkflowCompiler().compile(definition)
    async with SessionLocal() as session:
        component = await session.scalar(select(Component).where(Component.name == "确定性问候"))
        if component is None:
            component = await _create_component(
                session,
                name="确定性问候",
                description="输入姓名，由 Direct 节点生成结构化问候；不加载任何模型。",
                workflow_name="问候工作流",
                definition=definition,
                validation={
                    "topologicalOrder": compiled.topological_order,
                    "diagnostics": [],
                    "executionClass": "zero_llm",
                },
            )
        weather_component = await session.scalar(
            select(Component).where(Component.name == "本地天气查询")
        )
        if weather_component is None:
            server_file = Path(__file__).resolve().parents[2] / "examples" / "local_weather_mcp.py"
            _, server_version = await register_server(
                session,
                "local-weather",
                "stdio",
                {
                    "command": [sys.executable, str(server_file)],
                    "timeoutSeconds": 5,
                },
            )
            snapshot = (await discover_tools(session, server_version.id))[0]
            weather_definition = _weather_workflow(snapshot.id)
            weather_compiled = WorkflowCompiler().compile(weather_definition)
            await _create_component(
                session,
                name="本地天气查询",
                description="固定 MCP Tool 查询本地确定性天气数据，模型调用为零。",
                workflow_name="天气查询工作流",
                definition=weather_definition,
                validation={
                    "topologicalOrder": weather_compiled.topological_order,
                    "diagnostics": [],
                    "executionClass": "zero_llm",
                    "mcpLocks": [
                        {
                            "nodeId": "weather",
                            "mcpToolSnapshotId": snapshot.id,
                            "digest": snapshot.digest,
                        }
                    ],
                },
            )
        await session.commit()
        return component.id


async def _create_component(
    session,
    *,
    name: str,
    description: str,
    workflow_name: str,
    definition: dict,
    validation: dict,
) -> Component:
    compiled = WorkflowCompiler().compile(definition)
    workflow = Workflow(
        id=str(uuid4()),
        name=workflow_name,
        owner_id="local-user",
        draft_revision=1,
        draft_json=definition,
    )
    session.add(workflow)
    await session.flush()
    version = WorkflowVersion(
        id=str(uuid4()),
        workflow_id=workflow.id,
        version=1,
        definition_json=definition,
        digest=compiled.digest,
        validation_json=validation,
    )
    session.add(version)
    await session.flush()
    component = Component(
        id=str(uuid4()),
        name=name,
        description=description,
        workflow_id=workflow.id,
        default_version_id=version.id,
        visibility="public",
        execution_class=validation.get("executionClass", "zero_llm"),
    )
    session.add(component)
    return component


def _weather_workflow(snapshot_id: str) -> dict:
    schema = {
        "type": "object",
        "properties": {
            "city": {"type": "string"},
            "condition": {"type": "string"},
            "temperature": {"type": "number"},
        },
        "required": ["city", "condition", "temperature"],
        "additionalProperties": False,
    }
    return {
        "apiVersion": "workflow.platform/v1",
        "kind": "Workflow",
        "metadata": {"name": "local-weather"},
        "spec": {
            "inputSchema": {
                "type": "object",
                "properties": {"city": {"type": "string", "title": "城市"}},
                "required": ["city"],
                "additionalProperties": False,
            },
            "outputSchema": schema,
            "nodes": [
                {"id": "input", "type": "input", "displayName": "输入"},
                {
                    "id": "weather",
                    "type": "mcp",
                    "displayName": "查询天气",
                    "input": {"city": "${input.city}"},
                    "config": {"mcpToolSnapshotId": snapshot_id},
                },
                {
                    "id": "output",
                    "type": "output",
                    "displayName": "输出",
                    "input": {
                        "city": "${weather.output.city}",
                        "condition": "${weather.output.condition}",
                        "temperature": "${weather.output.temperature}",
                    },
                },
            ],
            "edges": [
                {"source": "input", "target": "weather"},
                {"source": "weather", "target": "output"},
            ],
        },
    }


def main() -> None:
    component_id = asyncio.run(seed())
    print(f"seeded component: {component_id}")


if __name__ == "__main__":
    main()
