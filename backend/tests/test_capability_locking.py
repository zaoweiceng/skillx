import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio

from app.application.capabilities import (
    CapabilityResolutionError,
    register_skillx,
    validate_workflow_capabilities,
)
from app.application.compiler import WorkflowCompiler, WorkflowValidationError
from app.application.orchestrator import Orchestrator, WorkerService
from app.config import get_settings
from app.domain.workflow import WorkflowDefinition
from app.infrastructure.database import Run, SessionLocal, Workflow, WorkflowVersion
from app.runtime.skillx import SkillXValidationError


@pytest_asyncio.fixture
async def capability_root(tmp_path: Path) -> AsyncIterator[Path]:
    source = Path(__file__).resolve().parents[2] / "capabilities" / "skillx" / "text-stats"
    root = tmp_path / "capabilities"
    shutil.copytree(source, root / "text-stats")
    settings = get_settings()
    previous = settings.capability_root
    settings.capability_root = root
    try:
        yield root
    finally:
        settings.capability_root = previous


def skillx_workflow(version_id: str) -> dict[str, Any]:
    return {
        "apiVersion": "workflow.platform/v1",
        "kind": "Workflow",
        "metadata": {"name": "text-stats"},
        "spec": {
            "inputSchema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            },
            "outputSchema": {
                "type": "object",
                "properties": {
                    "characters": {"type": "integer"},
                    "nonWhitespaceCharacters": {"type": "integer"},
                    "words": {"type": "integer"},
                },
                "required": ["characters", "nonWhitespaceCharacters", "words"],
                "additionalProperties": False,
            },
            "nodes": [
                {"id": "input", "type": "input", "displayName": "输入"},
                {
                    "id": "count",
                    "type": "skillx",
                    "displayName": "文本统计",
                    "input": {"text": "${input.text}"},
                    "config": {
                        "capabilityVersionId": version_id,
                        "interface": "count",
                    },
                },
                {
                    "id": "output",
                    "type": "output",
                    "displayName": "输出",
                    "input": {
                        "characters": "${count.output.characters}",
                        "nonWhitespaceCharacters": "${count.output.nonWhitespaceCharacters}",
                        "words": "${count.output.words}",
                    },
                },
            ],
            "edges": [
                {"source": "input", "target": "count"},
                {"source": "count", "target": "output"},
            ],
        },
    }


async def test_registration_rejects_path_outside_capability_root(
    capability_root: Path,
) -> None:
    async with SessionLocal() as session:
        with pytest.raises(SkillXValidationError):
            await register_skillx(session, str(capability_root.parent.resolve()))


async def test_workflow_rejects_mutable_package_path() -> None:
    definition = skillx_workflow("cap-version")
    definition["spec"]["nodes"][1]["config"]["package"] = "/tmp/untrusted"

    with pytest.raises(WorkflowValidationError) as error:
        WorkflowCompiler().compile(definition)

    assert "SKILLX_PACKAGE_PATH_FORBIDDEN" in {item.code for item in error.value.diagnostics}


async def test_locked_skillx_runs_in_subprocess_without_model(
    capability_root: Path,
) -> None:
    async with SessionLocal() as session, session.begin():
        _, capability_version = await register_skillx(session, "text-stats")
        definition = skillx_workflow(capability_version.id)
        compiled = WorkflowCompiler().compile(definition)
        execution_class, locks = await validate_workflow_capabilities(session, compiled.definition)
        assert execution_class == "zero_llm"
        assert locks[0]["digest"] == capability_version.digest
        workflow = Workflow(
            id=str(uuid4()),
            name="text-stats",
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
                "executionClass": execution_class,
                "capabilityLocks": locks,
            },
        )
        session.add(version)
        await session.flush()
        run = await Orchestrator().create_run(session, version, {"text": "hello 世界"})
        run_id = run.id

    worker = WorkerService("skillx-worker")
    assert await worker.run_once()
    assert await worker.run_once()
    assert await worker.run_once()

    async with SessionLocal() as session:
        completed = await session.get(Run, run_id)
        assert completed is not None
        assert completed.status == "succeeded"
        assert completed.output_json == {
            "characters": 8,
            "nonWhitespaceCharacters": 7,
            "words": 2,
        }


async def test_package_digest_change_blocks_historical_execution(
    capability_root: Path,
) -> None:
    async with SessionLocal() as session, session.begin():
        _, version = await register_skillx(session, "text-stats")
        definition = WorkflowDefinition.model_validate(skillx_workflow(version.id))

    executor = capability_root / version.package_path / "executors" / "count.py"
    executor.write_text(executor.read_text() + "\n# changed\n", encoding="utf-8")

    async with SessionLocal() as session:
        with pytest.raises(CapabilityResolutionError):
            await validate_workflow_capabilities(session, definition)
