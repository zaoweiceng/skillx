from __future__ import annotations

import os
import shutil
import time
from collections.abc import Iterator

import httpx
import pytest

from e2e.harness import DemoStack, poll_conversation_run, poll_run, start_stack, stop_stack


@pytest.fixture(scope="module")
def real_product_stack() -> Iterator[DemoStack]:
    if os.environ.get("SKILLX_RUN_TRAEX_REAL_PRODUCT_CHECK") != "1":
        pytest.skip(
            "blocked: set SKILLX_RUN_TRAEX_REAL_PRODUCT_CHECK=1 to authorize the real product gate"
        )
    executable = shutil.which("traex")
    if executable is None:
        pytest.skip("blocked: traex is not installed on PATH")
    stack = start_stack(
        agent_fixture=False,
        model_env={
            "SKILLX_AGENT_BUILTIN_ENABLED": "true",
            "SKILLX_AGENT_RUNTIME_TYPE": "unavailable",
            "SKILLX_PROVIDER_PROBE_TIMEOUT_SECONDS": "180",
            "SKILLX_AGENT_MAX_DURATION_SECONDS": "180",
        }
    )
    try:
        yield stack
    finally:
        stop_stack(stack)


def _client(stack: DemoStack) -> httpx.Client:
    return httpx.Client(base_url=stack.base_url, timeout=240)


@pytest.fixture(scope="module")
def probed_real_product_stack(real_product_stack: DemoStack) -> DemoStack:
    with _client(real_product_stack) as client:
        health = client.get("/health/ready")
        assert health.status_code == 200, health.text
        runtime = health.json()["agentRuntime"]
        assert runtime["configured"] is True, runtime
        assert runtime["runtimeType"] == "traex_builtin", runtime
        assert runtime["source"] == "auto_discovered", runtime
        probe = client.post("/health/provider-probe")
        assert probe.status_code == 200, probe.text
        assert probe.json()["ok"] is True, probe.json()
    return real_product_stack


def test_real_product_workflow_on(probed_real_product_stack: DemoStack) -> None:
    with _client(probed_real_product_stack) as client:
        enabled = client.post("/conversations", json={"title": "real-workflow-on"}).json()
        selected = client.post(
            f"/conversations/{enabled['id']}/messages",
            json={"content": "请问候名叫 Alice 的人，姓名参数是 Alice"},
        )
        assert selected.status_code == 201, selected.text
        selected_assistant = selected.json()["assistant"]
        assert selected_assistant["state"] == "workflow_run", selected_assistant
        assert selected_assistant["bindingSources"]
        assert selected_assistant["bindingSources"][0]["serverVerified"] is True
        workflow_run = poll_run(client, selected_assistant["runId"], timeout=120)
        assert workflow_run["status"] == "succeeded", workflow_run
        assert workflow_run["output"] == {"message": "你好，Alice"}
        refreshed = client.get(f"/conversations/{enabled['id']}").json()
        persisted = refreshed["messages"][-1]["content"]
        assert persisted["runId"] == selected_assistant["runId"]
        assert persisted["bindingSources"][0]["serverVerified"] is True


def test_real_product_workflow_off(probed_real_product_stack: DemoStack) -> None:
    with _client(probed_real_product_stack) as client:
        disabled = client.post("/conversations", json={"title": "real-workflow-off"}).json()
        mode = client.patch(
            f"/conversations/{disabled['id']}/workflow-mode",
            json={"workflowMode": "disabled"},
        )
        assert mode.status_code == 200, mode.text
        answered = client.post(
            f"/conversations/{disabled['id']}/messages",
            json={"content": "用一句话说明今天适合专注完成一件事"},
        )
        assert answered.status_code == 201, answered.text
        answer_id = answered.json()["assistant"]["conversationRunId"]
        answer = poll_conversation_run(client, answer_id, timeout=120)
        assert answer["status"] == "succeeded", answer
        assert isinstance(answer["output"].get("reply"), str)
        events = client.get(f"/conversation-runs/{answer_id}/events").json()
        assert not any(
            str(item.get("payload", {}).get("capabilityRef", "")).startswith(
                "workflow."
            )
            for item in events
        )


def test_real_product_running_cancellation(
    probed_real_product_stack: DemoStack,
) -> None:
    with _client(probed_real_product_stack) as client:
        conversation = client.post(
            "/conversations", json={"title": "real-cancellation"}
        ).json()
        started = client.post(
            f"/conversations/{conversation['id']}/agent-runs",
            json={
                "content": (
                    "请进行深入分析并列出一百个互不重复的工程风险，每项说明成因、"
                    "影响、检测办法和缓解措施"
                ),
                "allowedCapabilities": ["lookup.table"],
            },
        )
        assert started.status_code == 202, started.text
        run_id = started.json()["conversationRunId"]
        deadline = time.time() + 20
        state = "queued"
        while time.time() < deadline:
            state = client.get(f"/conversation-runs/{run_id}").json()["status"]
            if state == "running":
                break
            if state in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.05)
        assert state == "running", f"run reached {state} before cancellation"
        cancelled = client.post(f"/conversation-runs/{run_id}/cancel")
        assert cancelled.status_code == 202, cancelled.text
        terminal = poll_conversation_run(client, run_id, timeout=30)
        assert terminal["status"] == "cancelled", terminal
