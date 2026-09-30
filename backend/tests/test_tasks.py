from uuid import uuid4

import pytest
from sqlalchemy import select

from app.application.agent_host import AgentHost, AgentHostResult
from app.application.task_contracts import HumanRequest
from app.application.tasks import TaskDriver, answer_human, request_human
from app.infrastructure.database import SessionLocal
from app.infrastructure.models.tasks import HumanInteraction, Invocation, LearningJob, Task


async def make_task(goal="完成任务"):
    async with SessionLocal() as session, session.begin():
        task = Task(
            id=str(uuid4()),
            goal=goal,
            status="queued",
            state_json={},
            runtime_json={},
            output_json={},
        )
        session.add(task)
    return task.id


@pytest.mark.asyncio
async def test_human_answer_snapshot_idempotency_and_waiting_not_starve(monkeypatch):
    waiting_id = await make_task()
    ready_id = await make_task()
    async with SessionLocal() as session, session.begin():
        task = await session.get(Task, waiting_id)
        item = await request_human(
            session,
            task,
            HumanRequest(
                key="city",
                title="城市",
                reason="需要目的地",
                inputSchema={
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            ),
        )
        await session.flush()
        item_id, digest = item.id, item.snapshot_digest

    async def decide(self, request, **kwargs):
        return AgentHostResult(
            output={
                "action": "complete",
                "summary": "完成",
                "completionCriteria": ["回答"],
                "answer": "答案",
            }
        )

    monkeypatch.setattr(AgentHost, "execute", decide)
    assert await TaskDriver().poll_once()
    async with SessionLocal() as session, session.begin():
        assert (await session.get(Task, ready_id)).status == "succeeded"
        assert (await session.get(HumanInteraction, item_id)).status == "pending"
        with pytest.raises(ValueError, match="快照"):
            await answer_human(
                session,
                item_id,
                revision=1,
                snapshot_digest="stale",
                answer={"city": "北京"},
                key="answer",
            )
        first = await answer_human(
            session,
            item_id,
            revision=1,
            snapshot_digest=digest,
            answer={"city": "北京"},
            key="answer",
        )
        again = await answer_human(
            session,
            item_id,
            revision=1,
            snapshot_digest=digest,
            answer={"city": "北京"},
            key="answer",
        )
        assert first.id == again.id
        with pytest.raises(ValueError, match="已被回答"):
            await answer_human(
                session,
                item_id,
                revision=1,
                snapshot_digest=digest,
                answer={"city": "上海"},
                key="other",
            )


@pytest.mark.asyncio
async def test_successful_workflow_returns_to_goal_without_learning(monkeypatch):
    task_id = await make_task()
    async with SessionLocal() as session, session.begin():
        item = Invocation(
            id=str(uuid4()),
            task_id=task_id,
            scope=task_id,
            call_key="A",
            kind="workflow",
            status="succeeded",
            request_json={},
            output_json={"result": 42},
        )
        session.add(item)
        call_id = item.id

    async def decide(self, request, **kwargs):
        assert "42" in request.prompt
        return AgentHostResult(
            output={
                "action": "complete",
                "summary": "目标完成",
                "completionCriteria": ["得到结果"],
                "evidence": [call_id],
                "answer": "42",
            }
        )

    monkeypatch.setattr(AgentHost, "execute", decide)
    await TaskDriver().poll_once()
    async with SessionLocal() as session:
        assert (await session.get(Task, task_id)).status == "succeeded"
        assert not list(await session.scalars(select(LearningJob)))


@pytest.mark.asyncio
async def test_task_dispatch_and_question_persist_together(monkeypatch):
    task_id = await make_task()

    async def decide(self, request, **kwargs):
        return AgentHostResult(
            output={
                "action": "human.request",
                "summary": "先计算，再等待输入",
                "completionCriteria": ["计算并汇报"],
                "calls": [
                    {
                        "key": "calculate",
                        "kind": "code",
                        "arguments": {"language": "python", "source": "print(2+2)"},
                        "reason": "需要计算",
                    }
                ],
                "questions": [
                    {
                        "key": "format",
                        "title": "格式",
                        "reason": "选择结果格式",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"format": {"enum": ["json", "text"]}},
                            "required": ["format"],
                        },
                    }
                ],
            }
        )

    monkeypatch.setattr(AgentHost, "execute", decide)
    await TaskDriver().poll_once()
    async with SessionLocal() as session:
        assert (await session.get(Task, task_id)).status == "waiting_execution"
        assert len(list(await session.scalars(select(Invocation)))) == 1
        assert len(list(await session.scalars(select(HumanInteraction)))) == 1


@pytest.mark.asyncio
async def test_local_code_invocation_executes_and_exports_evidence(tmp_path, monkeypatch):
    from app.application.invocations import InvocationDriver, enqueue_invocation
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "artifact_root", tmp_path)
    task_id = await make_task()
    async with SessionLocal() as session, session.begin():
        task = await session.get(Task, task_id)
        item = await enqueue_invocation(
            session,
            kind="code",
            arguments={
                "language": "python",
                "source": 'import os\nprint(6*7)\nassert "SKILLX_AGENT_API_KEY" not in os.environ',
            },
            key="code",
            task=task,
        )
        item_id = item.id
    assert await InvocationDriver().poll_once()
    async with SessionLocal() as session:
        item = await session.get(Invocation, item_id)
        assert item.status == "succeeded", item.error_json
        assert item.output_json["stdout"].strip() == "42"
        assert (tmp_path / item.output_json["evidence"] / "completed.json").exists()


@pytest.mark.asyncio
async def test_nested_workflow_executes_pinned_child():
    from app.application.compiler import WorkflowCompiler
    from app.application.orchestrator import Orchestrator, WorkerService
    from app.application.workflow_nesting import expand_workflow
    from app.infrastructure.database import Run, Workflow, WorkflowVersion
    from tests.test_workflow_catalog_plans import _published

    _, child, _ = await _published()
    raw = {
        "apiVersion": "workflow.platform/v1",
        "kind": "Workflow",
        "metadata": {"name": "parent"},
        "spec": {
            "inputSchema": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
            "outputSchema": child.definition_json["spec"]["outputSchema"],
            "nodes": [
                {"id": "input", "type": "input", "displayName": "输入"},
                {
                    "id": "nested",
                    "type": "workflow_call",
                    "displayName": "子流程",
                    "input": {"name": "${input.name}"},
                    "config": {"workflowVersionId": child.id, "digest": child.digest},
                },
                {
                    "id": "output",
                    "type": "output",
                    "displayName": "输出",
                    "input": {"message": "${nested.output.message}"},
                },
            ],
            "edges": [
                {"source": "input", "target": "nested"},
                {"source": "nested", "target": "output"},
            ],
        },
    }
    async with SessionLocal() as session, session.begin():
        expanded = await expand_workflow(session, raw)
        compiled = WorkflowCompiler().compile(expanded)
        workflow = Workflow(id=str(uuid4()), name="parent", draft_json=raw, draft_revision=1)
        session.add(workflow)
        await session.flush()
        version = WorkflowVersion(
            id=str(uuid4()),
            workflow_id=workflow.id,
            version=1,
            definition_json=expanded,
            digest=compiled.digest,
            validation_json={},
        )
        session.add(version)
        await session.flush()
        run = await Orchestrator().create_run(session, version, {"name": "Ada"})
        run_id = run.id
    worker = WorkerService("nested-test")
    for _ in range(8):
        await worker.run_once()
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        assert run.status == "succeeded", run.error_json
        assert "Ada" in run.output_json["message"]


@pytest.mark.asyncio
async def test_generated_skill_contracts_are_executed_before_registration(tmp_path, monkeypatch):
    from app.application.task_learning import ContractExample, SkillProposal, materialize_skill
    from app.config import get_settings
    from app.runtime.skillx_host import execute_in_subprocess

    monkeypatch.setattr(get_settings(), "capability_root", tmp_path)
    source = Invocation(id="source", kind="tool", status="succeeded", output_json={"value": 4})
    proposal = SkillProposal(
        name="double-number",
        description="将数字翻倍",
        inputSchema={
            "type": "object",
            "properties": {"value": {"type": "number"}},
            "required": ["value"],
        },
        outputSchema={
            "type": "object",
            "properties": {"value": {"type": "number"}},
            "required": ["value"],
        },
        source='def execute(request, context):\n    return {"value": request.input["value"] * 2}\n',
        examples=[
            ContractExample(input={"value": 2}, expected={"value": 4}, sourceInvocationId="source"),
            ContractExample(input={"value": 3}, expected={"value": 6}),
        ],
    )
    root = await materialize_skill(proposal, str(uuid4()), {"source": source})
    result = await execute_in_subprocess(root, "execute", "different-input", {"value": 9})
    assert result.output == {"value": 18}


@pytest.mark.asyncio
async def test_manual_skillx_invocation_requires_explicit_approval(tmp_path, monkeypatch):
    import yaml

    from app.application.capabilities import register_skillx
    from app.application.invocations import InvocationDriver, enqueue_invocation
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "capability_root", tmp_path)
    package = tmp_path / "confirmed"
    package.mkdir()
    (package / "SKILL.md").write_text("---\nname: confirmed\ndescription: test\n---\n")
    (package / "input.json").write_text('{"type":"object"}')
    (package / "output.json").write_text('{"type":"object"}')
    (package / "main.py").write_text(
        'def execute(request, context):\n return {"confirmed": True}\n'
    )
    manifest = {
        "apiVersion": "skillx.dev/v1alpha1",
        "kind": "SkillX",
        "metadata": {"name": "confirmed", "version": "1.0.0", "description": "test"},
        "spec": {
            "guide": {"entry": "SKILL.md"},
            "defaultInterface": "run",
            "interfaces": {
                "run": {
                    "kind": "direct",
                    "description": "test",
                    "inputSchema": "input.json",
                    "outputSchema": "output.json",
                    "runtime": {
                        "protocol": "skillx.python-callable/v1",
                        "entry": "main.py:execute",
                    },
                    "execution": {"timeoutSeconds": 10},
                    "effects": [{"type": "data.write", "confirmation": "required"}],
                }
            },
        },
    }
    (package / "skillx.yaml").write_text(yaml.safe_dump(manifest))
    async with SessionLocal() as session, session.begin():
        _, version = await register_skillx(session, "confirmed")
        item = await enqueue_invocation(
            session,
            kind="skillx",
            arguments={"versionId": version.id, "interface": "run", "input": {}},
            key="manual",
        )
        item_id = item.id
    assert not await InvocationDriver().poll_once()
    async with SessionLocal() as session, session.begin():
        item = await session.get(Invocation, item_id)
        assert item.status == "waiting_user"
        question = await session.scalar(select(HumanInteraction))
        await answer_human(
            session,
            question.id,
            revision=1,
            snapshot_digest=question.snapshot_digest,
            answer={"approved": True},
            key="approve",
        )
    assert await InvocationDriver().poll_once()
    async with SessionLocal() as session:
        item = await session.get(Invocation, item_id)
        assert item.status == "succeeded", item.error_json


async def test_candidate_graph_validation_executes_edges_and_business_assertions():
    import json
    from pathlib import Path

    from app.application.learning_validation import validate_candidate
    from app.application.task_learning import ContractExample

    definition = json.loads(
        (Path(__file__).resolve().parents[2] / "examples/greeting-workflow.json").read_text()
    )
    source = Invocation(
        id="source", kind="tool", status="succeeded", output_json={"message": "你好，Alice"}
    )
    examples = [
        ContractExample(
            input={"name": "Alice"}, expected=source.output_json, sourceInvocationId="source"
        ),
        ContractExample(input={"name": "Bob"}, expected={"message": "你好，Bob"}),
    ]
    async with SessionLocal() as session:
        await validate_candidate(session, definition, examples, {"source": source})
        examples[1].expected = {"message": "错误结果"}
        with pytest.raises(ValueError, match="业务断言"):
            await validate_candidate(session, definition, examples, {"source": source})


@pytest.mark.parametrize("kind", ["llm", "agent"])
async def test_manual_model_skillx_uses_durable_invocation(kind, tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    import yaml

    from app.application.capabilities import register_skillx
    from app.application.invocations import InvocationDriver, enqueue_invocation
    from app.application.structured_generation import StructuredGenerationResolver
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "capability_root", tmp_path)
    package = tmp_path / "model-skill"
    package.mkdir()
    (package / "SKILL.md").write_text("---\nname: model-skill\ndescription: test\n---\n")
    (package / "prompt.md").write_text("Return a structured answer.")
    (package / "input.json").write_text('{"type":"object"}')
    (package / "output.json").write_text(
        json.dumps(
            {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}
        )
    )
    (package / "skillx.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "skillx.dev/v1alpha1",
                "kind": "SkillX",
                "metadata": {"name": "model-skill", "version": "1.0.0", "description": "test"},
                "spec": {
                    "guide": {"entry": "SKILL.md"},
                    "defaultInterface": "run",
                    "interfaces": {
                        "run": {
                            "kind": kind,
                            "description": "test",
                            "inputSchema": "input.json",
                            "outputSchema": "output.json",
                            "prompt": {"entry": "prompt.md"},
                            "execution": {"timeoutSeconds": 10},
                            "capabilities": [],
                        }
                    },
                },
            }
        )
    )
    calls = []

    async def generate(self, request, *args, **kwargs):
        calls.append(request)
        return SimpleNamespace(output={"answer": "ok"}, total_tokens=9)

    monkeypatch.setattr(AgentHost, "execute", generate)
    monkeypatch.setattr(StructuredGenerationResolver, "generate", generate)
    async with SessionLocal() as session, session.begin():
        _, version = await register_skillx(session, "model-skill")
        invocation = await enqueue_invocation(
            session,
            kind="skillx",
            key="model",
            arguments={"versionId": version.id, "interface": "run", "input": {}},
        )
        invocation_id = invocation.id
        assert invocation.status == "queued"
    assert not calls
    assert await InvocationDriver().poll_once()
    async with SessionLocal() as session:
        invocation = await session.get(Invocation, invocation_id)
        assert invocation.status == "succeeded", invocation.error_json
        assert invocation.output_json == {"answer": "ok"}
        assert (await session.get(Task, invocation.task_id)).total_tokens == 9
    assert len(calls) == 1


async def test_system_install_is_host_authorized_and_denial_is_not_reasked():
    from app.application.invocations import InvocationDriver, enqueue_invocation

    task_id = await make_task()
    arguments = {"manager": "npm", "packages": ["example@1.0.0"], "scope": "system"}
    async with SessionLocal() as session, session.begin():
        task = await session.get(Task, task_id)
        item = await enqueue_invocation(
            session, kind="install", arguments=arguments, key="install", task=task
        )
        assert item.status == "waiting_user"
        question = await session.scalar(select(HumanInteraction))
        assert question.kind == "authorization"
        assert question.request_json["operation"] == arguments
        await answer_human(
            session,
            question.id,
            revision=question.revision,
            snapshot_digest=question.snapshot_digest,
            answer={"approved": False},
            key="decline",
        )
    assert not await InvocationDriver().poll_once()
    async with SessionLocal() as session, session.begin():
        task = await session.get(Task, task_id)
        repeated = await enqueue_invocation(
            session, kind="install", arguments=arguments, key="new-key", task=task
        )
        assert repeated.status == "failed"
        assert repeated.error_json["code"] == "PERMISSION_DENIED"
        questions = list(await session.scalars(select(HumanInteraction)))
        assert len(questions) == 1


@pytest.mark.asyncio
async def test_cancelled_goal_does_not_kill_task_claim_loop(monkeypatch):
    import asyncio

    from app.application.tasks import cancel_task

    cancelled_id = await make_task("cancel me")
    next_id = await make_task("next task")
    attempts = 0

    async def execute(self, request, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            async with SessionLocal() as session, session.begin():
                await cancel_task(session, await session.get(Task, cancelled_id))
            raise asyncio.CancelledError
        return AgentHostResult(output={"action": "complete", "summary": "done",
                                      "answer": "done", "completionCriteria": ["done"],
                                      "evidence": []})

    monkeypatch.setattr(AgentHost, "execute", execute)
    driver = TaskDriver()
    assert await driver.poll_once()
    assert await driver.poll_once()
    async with SessionLocal() as session:
        assert (await session.get(Task, cancelled_id)).status == "cancelled"
        assert (await session.get(Task, next_id)).status == "succeeded"


@pytest.mark.asyncio
async def test_worker_shutdown_still_propagates_cancellation(monkeypatch):
    import asyncio
    task_id = await make_task()
    entered = asyncio.Event()

    async def execute(self, request, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(AgentHost, "execute", execute)
    poll = asyncio.create_task(TaskDriver().poll_once())
    await asyncio.wait_for(entered.wait(), 5)
    poll.cancel()
    with pytest.raises(asyncio.CancelledError):
        await poll

    async with SessionLocal() as session:
        task = await session.get(Task, task_id)
        assert task.status == "queued"
        assert task.lease_token is None


async def test_malformed_terminal_retries_are_bounded(monkeypatch):
    from app.runtime.traex_builtin import TraeXBridgeError

    task_id = await make_task()

    async def malformed(self, request, **kwargs):
        raise TraeXBridgeError("MODEL_MALFORMED_OUTPUT", "invalid terminal")

    monkeypatch.setattr(AgentHost, "execute", malformed)
    driver = TaskDriver()
    for expected in ("queued", "queued", "failed"):
        assert await driver.poll_once()
        async with SessionLocal() as session:
            assert (await session.get(Task, task_id)).status == expected


def test_observation_bounds_structured_results_without_mutating_evidence():
    from app.application.task_context import observation

    original = {"stdout": __import__('json').dumps({"files": ["file"] * 1000, "body": "x" * 5000})}
    result = observation(original)
    assert result["stdout"]["files"]["totalItems"] == 1000
    assert result["stdout"]["body"]["truncated"]
    assert len(original["stdout"]) > 10000


def test_code_call_rejects_nested_arguments_before_dispatch():
    from pydantic import ValidationError

    from app.application.task_contracts import TaskCall

    with pytest.raises(ValidationError, match="不能包在 input 中"):
        TaskCall(key="read", kind="code", reason="read files", arguments={
            "input": {"language": "python", "source": "print('ok')"}
        })
