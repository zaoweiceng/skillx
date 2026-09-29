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


def poll_task(client, task_id, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/tasks/{task_id}")
        assert response.status_code == 200, response.text
        task = response.json()
        if task["status"] in {"succeeded", "failed", "blocked", "cancelled", "waiting_user"}:
            return task
        time.sleep(0.25)
    raise AssertionError(f"Task timed out: {task}")


def test_real_product_workflow_on(probed_real_product_stack: DemoStack) -> None:
    with _client(probed_real_product_stack) as client:
        conversation = client.post("/conversations", json={"title": "real-auto-workflow"}).json()
        selected = client.post(
            f"/conversations/{conversation['id']}/messages",
            json={"content": "请问候名叫 Alice 的人，姓名参数是 Alice"},
        )
        assert selected.status_code == 202, selected.text
        task = poll_task(client, selected.json()["taskId"])
        assert task["status"] == "succeeded", task
        calls = [x for x in task["invocations"] if x.get("runId")]
        assert calls, task
        assert calls[0]["output"] == {"message": "你好，Alice"}
        assert not task["learning"], task
        assert "Alice" in task["output"]["text"]


def test_real_product_workflow_off(probed_real_product_stack: DemoStack) -> None:
    with _client(probed_real_product_stack) as client:
        conversation = client.post("/conversations", json={"title": "real-general-answer"}).json()
        answer = client.post(
            f"/conversations/{conversation['id']}/messages",
            json={"content": "用一句话说明今天适合专注完成一件事"},
        )
        assert answer.status_code == 202, answer.text
        task = poll_task(client, answer.json()["taskId"])
        assert task["status"] == "succeeded", task
        assert task["output"].get("text"), task


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
            f"/conversations/{source['id']}/agent-runs",
            json={
                "content": (
                    "请调用 lookup.table 工具查询 key 为 q2 的本地示例数据，"
                    "并用一句中文告诉我工具实际返回的结果。必须执行查询，不能凭记忆或猜测回答。"
                )
            },
        )
        assert explored.status_code == 202, explored.text
        conversation_run_id = explored.json()["conversationRunId"]
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
        assert selected.status_code == 202, selected.text
        task = poll_task(client, selected.json()["taskId"])
        assert task["status"] == "succeeded", task
        calls = [x for x in task["invocations"] if x.get("runId")]
        assert any(x["request"]["versionId"] == version_id for x in calls), task
        reused_run = poll_run(client, calls[0]["runId"], timeout=180)
        assert reused_run["status"] == "succeeded", reused_run
        assert "第三季度" in str(reused_run["output"]), reused_run
        assert "营收持平" in str(reused_run["output"]), reused_run


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


def test_real_product_selected_model(real_product_stack: DemoStack) -> None:
    """Discover and select a real model, then exercise the persisted execution path."""
    with _client(real_product_stack) as client:
        models = client.get("/runtime-profiles/traex/models")
        assert models.status_code == 200, models.text
        choices = models.json()
        assert choices
        selected = next(
            (item["id"] for item in choices if item["id"] == "Doubao-Seed-2.1-Turbo"),
            choices[0]["id"],
        )
        conversation = client.post("/conversations", json={"title": "real-model-choice"}).json()
        path = f"/runtime-profiles/conversations/{conversation['id']}"
        response = client.put(
            path, json={"agentProfileId": "builtin-traex", "traexModel": selected}
        )
        assert response.status_code == 200, response.text
        assert client.get(path).json()["traexModel"] == selected
        started = client.post(
            f"/conversations/{conversation['id']}/agent-runs",
            json={
                "content": "只用一句中文回复：模型选择已生效。",
                "allowedCapabilities": ["lookup.table"],
            },
        )
        assert started.status_code == 202, started.text
        run_id = started.json()["conversationRunId"]
        # Changing the conversation selection must not alter this already queued run.
        reset = client.put(path, json={"agentProfileId": "builtin-traex", "traexModel": None})
        assert reset.status_code == 200, reset.text
        result = poll_conversation_run(client, run_id, timeout=180)
        assert result["status"] == "succeeded", result
        assert result["executor"]["model"] == selected
        assert result["output"].get("reply"), result
        print(f"Real TraeX selected model verified: {selected}")


def test_real_task_human_and_multiple_dispatch(probed_real_product_stack: DemoStack) -> None:
    with _client(probed_real_product_stack) as client:
        conversation = client.post("/conversations", json={"title": "real-human-task"}).json()
        accepted = client.post(
            f"/conversations/{conversation['id']}/messages",
            json={
                "content": "先问我需要问候谁，收到姓名后执行问候工作流；"
                "然后根据问候结果，再调用一次同一个工作流问候 Bob，最后汇报两份结果。"
            },
        )
        assert accepted.status_code == 202, accepted.text
        task_id = accepted.json()["taskId"]
        waiting = poll_task(client, task_id)
        assert waiting["status"] == "waiting_user", waiting
        question = next(q for q in waiting["interactions"] if q["status"] == "pending")
        fields = question["request"]["inputSchema"]["properties"]
        answer = {name: "Alice" for name in fields}
        response = client.post(
            f"/human-interactions/{question['id']}/answer",
            json={
                "revision": question["revision"],
                "snapshotDigest": question["snapshotDigest"],
                "idempotencyKey": "real-answer",
                "answer": answer,
            },
        )
        assert response.status_code == 200, response.text
        task = poll_task(client, task_id)
        assert task["status"] == "succeeded", task
        calls = [c for c in task["invocations"] if c.get("runId")]
        assert len(calls) == 2, task
        assert {c["request"]["input"]["name"] for c in calls} == {"Alice", "Bob"}
        assert not task["learning"]


def test_real_task_code_and_skillx_learning(probed_real_product_stack: DemoStack) -> None:
    with _client(probed_real_product_stack) as client:
        conversation = client.post("/conversations", json={"title": "real-code-learning"}).json()
        response = client.post(
            f"/conversations/{conversation['id']}/messages",
            json={
                "content": "以后会反复需要这个确定性业务计算：输入整数 units 和整数 "
                "unit_price，输出 total=units*unit_price。请通过生成并实际执行 Python "
                "代码算 units=7、unit_price=13 的结果，标准输出为 JSON 对象 {total:91}。"
            },
        )
        assert response.status_code == 202, response.text
        task_id = response.json()["taskId"]
        task = poll_task(client, task_id)
        assert task["status"] == "succeeded", task
        assert any(
            c["kind"] == "code" and c["status"] == "succeeded" for c in task["invocations"]
        ), task
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            task = client.get(f"/tasks/{task_id}").json()
            jobs = task["learning"]
            if jobs and jobs[0]["status"] in {"published", "blocked", "skipped"}:
                break
            time.sleep(0.5)
        assert jobs and jobs[0]["status"] == "published", jobs
        version = jobs[0]["result"]["workflowVersionId"]
        manual = client.post(
            f"/workflow-versions/{version}/runs", json={"input": {"units": 3, "unit_price": 11}}
        )
        assert manual.status_code == 202, manual.text
        run = poll_run(client, manual.json()["id"], timeout=60)
        assert run["status"] == "succeeded", run
        assert run["output"] == {"total": 33}, run


def test_real_model_skill_materialization_and_manual_execution(probed_real_product_stack):
    import json
    import subprocess
    import sys
    from pathlib import Path

    backend = Path(__file__).resolve().parents[1]
    probe = subprocess.run(
        [sys.executable, "-m", "real_provider_tests.model_skill_probe"],
        cwd=backend, env=probed_real_product_stack._env,
        capture_output=True, text=True, timeout=400,
    )
    assert probe.returncode == 0, probe.stderr[-4000:]
    published = json.loads(probe.stdout.strip().splitlines()[-1])
    assert published["proof"]["status"] == "passed"
    assert len(published["proof"]["examples"]) == 2
    with _client(probed_real_product_stack) as client:
        response = client.post(f"/capability-versions/{published['versionId']}/invocations", json={
            "interface": "execute", "input": {"text": "This is wonderful!"},
            "idempotencyKey": "model-probe-manual",
        })
        assert response.status_code == 202, response.text
        invocation_id = response.json()["id"]
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            item = client.get(f"/invocations/{invocation_id}").json()
            if item["status"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.25)
        assert item["status"] == "succeeded", item
        assert item["output"] == {"sentiment": "positive"}, item
