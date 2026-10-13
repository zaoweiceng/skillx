import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from app.api.monitoring import metric_value, session_records, sessions, window
from app.application.product_history import record_model_usage, usage_summary
from app.application.session_records import record, recorded_session
from app.config import get_settings
from app.infrastructure.database import Conversation, ConversationTurn, SessionLocal
from app.infrastructure.models.tasks import Invocation, Task, TaskEvent
from app.runtime.traex_builtin import _usage


async def test_monitoring_counts_terminals_boundaries_duration_and_usage():
    start = datetime(2026, 10, 10, tzinfo=UTC)
    end = start + timedelta(days=1)
    async with SessionLocal() as db, db.begin():
        conversation = Conversation(id=str(uuid4()), title="monitor", workflow_mode="enabled")
        db.add(conversation)
        await db.flush()
        for index, status in enumerate(["succeeded", "failed", "blocked", "cancelled", "running"]):
            turn = ConversationTurn(
                id=str(uuid4()),
                conversation_id=conversation.id,
                workflow_mode_snapshot="enabled",
                status=status,
                accepted_at=start,
                completed_at=start + timedelta(seconds=(index + 1) * 10) if index < 4 else None,
            )
            db.add(turn)
            await db.flush()
            task = Task(
                id=str(uuid4()),
                conversation_id=conversation.id,
                turn_id=turn.id,
                status=status,
                goal="goal",
                total_tokens=10,
                created_at=start,
                error_json={"code": "TIMEOUT", "message": "Timed out"} if index == 1 else None,
                output_json={"text": "缺少必要输入"} if index == 2 else {},
            )
            db.add(task)
            await db.flush()
            if index == 0:
                db.add(
                    TaskEvent(
                        task_id=task.id,
                        type="agent.audit",
                        payload_json={
                            "type": "agent.step.completed",
                            "payload": {
                                "requestId": "unique",
                                "inputTokens": 8,
                                "outputTokens": 2,
                                "cacheReadTokens": 6,
                                "usageComplete": True,
                            },
                        },
                    )
                )
                first_turn = turn.id
        # End is exclusive and start inclusive.
        db.add(Task(id=str(uuid4()), status="failed", goal="outside", created_at=end))
        for index, kind in enumerate(["skillx", "workflow", "code"]):
            db.add(
                Invocation(
                    id=str(uuid4()),
                    scope="test",
                    call_key=str(index),
                    kind=kind,
                    request_json={},
                    status="failed",
                    created_at=start,
                )
            )
        await record_model_usage(
            db,
            call_id="standalone",
            stage="model",
            input_tokens=100,
            output_tokens=20,
            cached_tokens=80,
            reasoning_tokens=10,
            usage_complete=True,
        )
    async with SessionLocal() as db:
        success = await metric_value("success", start, end, db)
        assert success["value"] == pytest.approx(1 / 3)
        assert success["cancelled"] == 1 and success["tasks"] == 5
        assert (await metric_value("failure", start, end, db))["value"] == pytest.approx(2 / 3)
        duration = await metric_value("duration", start, end, db)
        assert duration["value"] == 20000
        assert [row["durationMs"] for row in duration["ranking"]] == [30000, 20000, 10000]
        assert [row["status"] for row in duration["ranking"]] == ["blocked", "failed", "succeeded"]
        assert all(row["conversationId"] == conversation.id for row in duration["ranking"])
        assert (await metric_value("skillx", start, end, db))["value"] == 1
        assert (await metric_value("workflow", start, end, db))["value"] == 1
        assert (await metric_value("tokens", start, end, db))["value"] == 50
        reasons = (await metric_value("reasons", start, end, db))["items"]
        assert {r["reason"] for r in reasons} == {"TIMEOUT", "缺少必要输入"}
        summary = await usage_summary(db, first_turn)
        assert summary["stages"][0]["cacheReadTokens"] == 6
        assert summary["totalTokens"] == 10
        empty = await metric_value("success", end + timedelta(days=1), end + timedelta(days=2), db)
        assert empty["value"] is None


def test_date_validation_and_traex_cache_semantics():
    with pytest.raises(HTTPException):
        window(datetime(2026, 1, 1), datetime(2026, 1, 2))
    with pytest.raises(HTTPException):
        window(datetime(2026, 1, 2, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC))
    assert _usage(
        {"usage": {"input_tokens": 100, "output_tokens": 20, "cached_input_tokens": 80}}
    ) == {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120, "cached_tokens": 80}
    assert "cached_tokens" not in _usage({"usage": {"input_tokens": 100}})


async def test_sessions_concurrent_failure_redaction_and_pagination(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "artifact_root", tmp_path)

    class Host:
        @recorded_session
        async def execute(self, request):
            await record(
                "event_msg",
                {
                    "run": request.run_id,
                    "arguments": {"api_key": "private"},
                    "text": "Authorization: Bearer private-token",
                },
            )
            if request.run_id == "failed":
                raise ValueError("failure detail")
            return SimpleNamespace(output={"result": "ok"}, total_tokens=10, usage_complete=True)

    def request(run):
        return SimpleNamespace(
            run_id=run,
            working_directory=tmp_path,
            prompt="hello",
            output_schema={},
            requested_capabilities=[],
        )

    results = await asyncio.gather(
        Host().execute(request("ok")), Host().execute(request("failed")), return_exceptions=True
    )
    assert isinstance(results[1], ValueError)
    listing = sessions(None, None, 0, 30)
    assert listing["total"] == 2
    for item in listing["items"]:
        first = session_records(UUID(item["id"]), 0, 2)
        rest = session_records(UUID(item["id"]), first["nextOffset"], 10)
        assert first["items"][0]["type"] == "session_meta"
        assert rest["items"][0]["payload"]["run"] == item["runId"]
        assert rest["items"][-1]["payload"]["status"] == (
            "failed" if item["runId"] == "failed" else "succeeded"
        )
        encoded = json.dumps(rest)
        assert "private" not in encoded
    with pytest.raises(HTTPException) as error:
        session_records(uuid4(), 0, 50)
    assert error.value.status_code == 404


async def test_call_counts_include_standalone_workflows_and_deduplicate_node_retries():
    from app.infrastructure.database import NodeRun, Run, Workflow, WorkflowVersion

    start = datetime.now(UTC) - timedelta(hours=1)
    end = start + timedelta(days=1)
    async with SessionLocal() as db, db.begin():
        workflow = Workflow(id=str(uuid4()), name="counts", draft_revision=1, draft_json={})
        db.add(workflow)
        await db.flush()
        version = WorkflowVersion(
            id=str(uuid4()),
            workflow_id=workflow.id,
            version=1,
            definition_json={},
            digest="counts",
            validation_json={},
        )
        db.add(version)
        await db.flush()
        runs = [
            Run(id=str(uuid4()), workflow_version_id=version.id, input_json={}, status="succeeded")
            for _ in range(2)
        ]
        db.add_all(runs)
        await db.flush()
        db.add(
            Invocation(
                id=str(uuid4()),
                scope="counts",
                call_key="linked",
                kind="workflow",
                run_id=runs[0].id,
                request_json={},
            )
        )
        db.add(
            Invocation(
                id=str(uuid4()), scope="counts", call_key="skill", kind="skillx", request_json={}
            )
        )
        for attempt in (1, 2):
            db.add(
                NodeRun(
                    id=str(uuid4()),
                    run_id=runs[0].id,
                    node_id="skill",
                    node_type="skillx",
                    attempt=attempt,
                    input_json={},
                    status="failed" if attempt == 1 else "succeeded",
                    started_at=start + timedelta(minutes=1),
                )
            )
    async with SessionLocal() as db:
        assert (await metric_value("workflow", start, end, db))["value"] == 2
        assert (await metric_value("skillx", start, end, db))["value"] == 2


def test_time_series_granularity_timezone_and_missing_values():
    from app.application.monitoring_series import TimeSeries

    start = datetime(2026, 10, 10, 16, tzinfo=UTC)  # Shanghai midnight.
    for hours, step in [
        (1, 300),
        (3, 900),
        (12, 1800),
        (24, 3600),
        (72, 3600),
        (168, 86400),
        (24 * 365, 5 * 86400),
    ]:
        series = TimeSeries(start, start + timedelta(hours=hours), 480)
        assert series.interval == step
        assert len(series.sums) <= 91
    daily = TimeSeries(start, start + timedelta(days=7), 480)
    daily.add(start - timedelta(seconds=1), 999)
    daily.add(start, 10)
    daily.add(start + timedelta(days=1), 20)
    daily.add(start + timedelta(days=7), 999)
    view = daily.view()["series"]
    assert [p["value"] for p in view] == [10, 20, 0, 0, 0, 0, 0]
    assert view[0]["start"] == "2026-10-10T16:00:00Z"
    assert daily.view(average=True)["series"][2]["value"] is None


async def test_hourly_rates_are_weighted_and_token_series_matches_total():
    start = datetime(2026, 10, 10, tzinfo=UTC)
    end = start + timedelta(days=1)
    async with SessionLocal() as db, db.begin():
        for index, status in enumerate(["succeeded", "failed", "failed", "failed", "running"]):
            db.add(
                Task(
                    id=str(uuid4()),
                    status=status,
                    goal="bucket",
                    total_tokens=100,
                    created_at=start + timedelta(hours=0 if index == 0 else 1),
                )
            )
    async with SessionLocal() as db:
        rates = await metric_value("success", start, end, db)
        assert len(rates["series"]) == 24
        assert rates["value"] == 0.25
        assert [p["value"] for p in rates["series"][:3]] == [1, 0, None]
        assert rates["series"][1]["samples"] == 3
        tokens = await metric_value("tokens", start, end, db)
        assert [p["value"] for p in tokens["series"][:3]] == [100, 400, 0]
        assert sum(p["value"] for p in tokens["series"]) == tokens["value"] == 500
