from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from app.application.compiler import WorkflowCompiler
from app.infrastructure.database import (
    Component,
    SessionLocal,
    Workflow,
    WorkflowVersion,
    init_database,
)


async def seed() -> str:
    await init_database()
    example = Path(__file__).resolve().parents[2] / "examples" / "greeting-workflow.json"
    definition = json.loads(example.read_text(encoding="utf-8"))
    compiled = WorkflowCompiler().compile(definition)
    async with SessionLocal() as session:
        existing = await session.scalar(select(Component).where(Component.name == "确定性问候"))
        if existing:
            return existing.id
        workflow = Workflow(
            id=str(uuid4()),
            name="问候工作流",
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
            validation_json={
                "topologicalOrder": compiled.topological_order,
                "diagnostics": [],
            },
        )
        session.add(version)
        await session.flush()
        component = Component(
            id=str(uuid4()),
            name="确定性问候",
            description="输入姓名，由 Direct 节点生成结构化问候；不加载任何模型。",
            workflow_id=workflow.id,
            default_version_id=version.id,
            visibility="public",
        )
        session.add(component)
        await session.commit()
        return component.id


def main() -> None:
    component_id = asyncio.run(seed())
    print(f"seeded component: {component_id}")


if __name__ == "__main__":
    main()
