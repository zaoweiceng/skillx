from __future__ import annotations

import os
import shutil
import time
from collections.abc import Iterator

import httpx
import pytest

from e2e.harness import DemoStack, poll_conversation_run, poll_run, start_stack, stop_stack


@pytest.fixture(scope="module")
def real_product_stack(request: pytest.FixtureRequest) -> Iterator[DemoStack]:
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
        },
    )
    try:
        yield stack
    finally:
        stop_stack(stack, failed=request.session.testsfailed > 0)


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
        assert runtime["source"] == "profile", runtime
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
            str(item.get("payload", {}).get("capabilityRef", "")).startswith("workflow.")
            for item in events
        )


def test_real_product_running_cancellation(
    probed_real_product_stack: DemoStack,
) -> None:
    with _client(probed_real_product_stack) as client:
        conversation = client.post("/conversations", json={"title": "real-cancellation"}).json()
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


def test_real_product_distill_publish_discover_and_reuse(
    probed_real_product_stack: DemoStack,
) -> None:
    """真实主闭环：探索、固化、发布、不同输入执行及新对话发现同一 v1。"""

    with _client(probed_real_product_stack) as client:
        source = client.post("/conversations", json={"title": "real-distill-source"}).json()
        disabled = client.patch(
            f"/conversations/{source['id']}/workflow-mode",
            json={"workflowMode": "disabled"},
        )
        assert disabled.status_code == 200, disabled.text
        explored = client.post(
            f"/conversations/{source['id']}/messages",
            json={
                "content": (
                    "请调用 lookup.table 工具查询 key 为 q2 的本地示例数据，"
                    "并用一句中文告诉我工具实际返回的结果。必须执行查询，不能凭记忆或猜测回答。"
                )
            },
        )
        assert explored.status_code == 201, explored.text
        conversation_run_id = explored.json()["assistant"]["conversationRunId"]
        exploration = poll_conversation_run(client, conversation_run_id, timeout=180)
        assert exploration["status"] == "succeeded", exploration.get("error")
        events = client.get(f"/conversation-runs/{conversation_run_id}/events").json()
        assert any(
            item.get("type") == "tool.result"
            and item.get("payload", {}).get("capabilityRef") == "lookup.table"
            and item.get("payload", {}).get("status") == "succeeded"
            for item in events
        ), events

        distilled = client.post(f"/conversation-runs/{conversation_run_id}/distill")
        assert distilled.status_code == 201, distilled.text
        distillation = distilled.json()
        assert distillation["status"] == "draft_created", distillation
        assert distillation["analysis"]["publishable"] is True, distillation
        workflow_id = distillation["draftWorkflowId"]
        workflow = client.get(f"/workflows/{workflow_id}").json()
        definition = workflow["definition"]
        node_types = {node["type"] for node in definition["spec"]["nodes"]}
        assert {"transform", "llm"} <= node_types, definition
        # This gate intentionally verifies TraeX only, without a Direct LLM service.
        # Explicitly choose an Agent node before publication; never use silent fallback.
        for node in definition["spec"]["nodes"]:
            if node["type"] == "llm":
                node["type"] = "agent"
                node["config"].update({"maxSteps": 1, "allowedCapabilities": []})
                node["config"].pop("modelProfile", None)
        definition["metadata"]["presentationPolicy"] = {
            "enabled": False,
            "audience": "internal",
            "producer": "template",
            "template": "执行结果：{json}",
        }
        definition["metadata"]["presentationPolicyDecision"] = {
            "status": "rejected",
            "reviewer": "real-product-gate",
            "reviewedAt": "2026-08-13T00:00:00Z",
        }
        updated = client.put(
            f"/workflows/{workflow_id}/draft",
            json={"revision": workflow["draftRevision"], "definition": definition},
        )
        assert updated.status_code == 200, updated.text
        updated_workflow = updated.json()
        validation = client.post(f"/workflows/{workflow_id}/validate")
        assert validation.status_code == 200, validation.text
        assert validation.json()["valid"] is True, validation.json()
        published = client.post(
            f"/workflows/{workflow_id}/publish",
            json={
                "draftRevision": updated_workflow["draftRevision"],
                "draftDigest": updated_workflow["draftDigest"],
            },
        )
        assert published.status_code == 201, published.text
        published_version = published.json()
        version_id = published_version["id"]

        manual = client.post(f"/workflow-versions/{version_id}/runs", json={"input": {"key": "q4"}})
        assert manual.status_code == 202, manual.text
        manual_run = poll_run(client, manual.json()["id"], timeout=180)
        assert manual_run["status"] == "succeeded", manual_run
        llm_node = next(node for node in manual_run["nodes"] if node["type"] == "agent")
        assert llm_node["status"] == "succeeded", llm_node
        assert any(llm_node["input"].values()), llm_node
        manual_reply = str(manual_run["output"].get("reply", ""))
        assert "第四季度" in manual_reply, manual_run["output"]
        assert "15%" in manual_reply, manual_run["output"]

        reuse = client.post("/conversations", json={"title": "real-distill-reuse"}).json()
        selected = client.post(
            f"/conversations/{reuse['id']}/messages",
            json={"content": "查询标识 q3 对应的本地示例数据并用一句中文告诉我结果"},
        )
        assert selected.status_code == 201, selected.text
        assistant = selected.json()["assistant"]
        assert assistant["state"] == "workflow_run", assistant
        assert assistant["agentAction"] in {"run_full", "run_fragment", "compose"}
        successful_results = [
            item
            for item in assistant["toolTimeline"]
            if item["type"] == "tool.result" and item["status"] == "succeeded"
        ]
        levels = {item.get("level") for item in successful_results}
        assert {"L0", "L1"} <= levels, assistant["toolTimeline"]
        if assistant["agentAction"] != "run_full":
            assert "L2" in levels, assistant["toolTimeline"]
        assert assistant["bindingSources"], assistant
        assert all(source["serverVerified"] is True for source in assistant["bindingSources"]), (
            assistant["bindingSources"]
        )
        # Agent 可选择直接 run_full，也可生成锁定同一版本的 plan/fragment；不可变 ref
        # 才是两种执行形态共同的版本身份。
        assert any(
            ref == published_version["workflowRef"] or workflow_id in ref
            for ref in assistant["refs"]
        ), assistant
        reused_run = poll_run(client, assistant["runId"], timeout=180)
        assert reused_run["status"] == "succeeded", reused_run
        reused_reply = str(reused_run["output"].get("reply", ""))
        assert "第三季度" in reused_reply, reused_run["output"]
        assert "营收持平" in reused_reply, reused_run["output"]
        refreshed = client.get(f"/conversations/{reuse['id']}").json()
        assert refreshed["messages"][-1]["content"]["runId"] == assistant["runId"]


def test_real_product_checkpoint_recovery(probed_real_product_stack: DemoStack) -> None:
    """Actual TraeX repairs a failed node, preserving the committed predecessor."""
    from app.seed import _recovery_workflow

    definition = _recovery_workflow()
    for node in definition["spec"]["nodes"]:
        if node["id"] == "risky":
            node["config"]["outputSchema"] = {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            }
        if node["id"] == "recovery_agent":
            node["config"]["maxTotalTokens"] = 20000
    with _client(probed_real_product_stack) as client:
        created = client.post(
            "/workflows", json={"name": "real-recovery", "definition": definition}
        )
        assert created.status_code == 201, created.text
        workflow = created.json()
        published = client.post(
            f"/workflows/{workflow['id']}/publish",
            json={
                "draftRevision": workflow["draftRevision"],
                "draftDigest": workflow["draftDigest"],
            },
        )
        assert published.status_code == 201, published.text
        started = client.post(
            f"/workflow-versions/{published.json()['id']}/runs",
            json={"input": {"seed": "已核实的业务结果"}},
        )
        assert started.status_code == 202, started.text
        result = poll_run(client, started.json()["id"], timeout=180)
        assert result["status"] == "succeeded", result
        detail = client.get(f"/runs/{result['id']}").json()
        predecessors = [node for node in detail["nodes"] if node["nodeId"] == "sensitive"]
        assert len(predecessors) == 1 and predecessors[0]["status"] == "succeeded"
        assert detail["checkpoints"][0]["status"] == "applied"
        assert detail["checkpoints"][0]["recoveryNodeRunId"]
