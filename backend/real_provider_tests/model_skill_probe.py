"""Real model contract probe; launched against the temporary product database."""

import asyncio
import json
from uuid import uuid4

from app.application.agent_host import AgentHost, AgentHostRequest
from app.application.capabilities import register_skillx
from app.application.runtime_profiles import agent_snapshot, restore_agent_snapshot
from app.application.task_learning import SkillProposal, materialize_skill
from app.config import get_settings
from app.infrastructure.database import SessionLocal
from app.infrastructure.models.tasks import Invocation, Task


async def main():
    task_id = str(uuid4())
    schema = {
        "type": "object",
        "properties": {"sentiment": {"enum": ["positive", "negative"]}},
        "required": ["sentiment"],
        "additionalProperties": False,
    }
    prompt = "Classify the sentiment of input.text. Return positive or negative in the schema."
    async with SessionLocal() as session:
        runtime = await agent_snapshot(session, None)
    result = await AgentHost(runtime=restore_agent_snapshot(runtime)).execute(
        AgentHostRequest(
            run_id="model-original-" + task_id,
            working_directory=get_settings().artifact_root / "model-probe" / task_id,
            prompt=prompt + '\nINPUT={"text":"I love this product!"}',
            output_schema=schema,
            max_steps=1,
            max_duration_seconds=120,
        )
    )
    assert result.output == {"sentiment": "positive"}, result.output
    async with SessionLocal() as session, session.begin():
        session.add(Task(id=task_id, goal="模型分类能力固化", status="manual"))
        await session.flush()
        witness = Invocation(
            id=str(uuid4()),
            task_id=task_id,
            scope=task_id,
            call_key="original",
            kind="skillx",
            status="succeeded",
            request_json={"input": {"text": "I love this product!"}},
            output_json=result.output,
            runtime_json=runtime,
        )
        session.add(witness)
    proposal = SkillProposal(
        name="model-sentiment-" + task_id[:8],
        kind="agent",
        description="情感分类",
        prompt=prompt,
        inputSchema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        outputSchema=schema,
        examples=[
            {
                "input": {"text": "I love this product!"},
                "expected": {"sentiment": "positive"},
                "sourceInvocationId": witness.id,
            },
            {"input": {"text": "I hate this product!"}, "expected": {"sentiment": "negative"}},
        ],
    )
    root = await materialize_skill(proposal, task_id, {witness.id: witness})
    async with SessionLocal() as session, session.begin():
        _, version = await register_skillx(
            session, str(root.relative_to(get_settings().capability_root.resolve()))
        )
        print(
            json.dumps(
                {
                    "versionId": version.id,
                    "proof": json.loads((root / "validation.json").read_text()),
                }
            )
        )


if __name__ == "__main__":
    asyncio.run(main())
