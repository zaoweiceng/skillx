"""六条 Demo E2E 黑盒验收。

每条场景都通过真实 HTTP API、独立 API/Worker 子进程和由 Alembic 建的临时 SQLite
完成。失败时 harness 会打印子进程日志。运行：
    uv run pytest e2e -q
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterator
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import httpx
import pytest

from e2e.harness import (
    DemoStack,
    poll_conversation_run,
    poll_run,
    restart_worker,
    start_stack,
    stop_stack,
)


class _LLMHandler(BaseHTTPRequestHandler):
    status = 200

    def do_POST(self) -> None:  # noqa: N802
        self.rfile.read(int(self.headers.get("content-length", "0")))
        if self.status != 200:
            self.send_response(self.status)
            self.end_headers()
            return
        payload = {
            "model": "e2e-direct-model",
            "choices": [{"message": {"content": json.dumps({"category": "direct"})}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3},
        }
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:
        return


class _LLMServer:
    def __init__(self, status: int) -> None:
        handler = type("ConfiguredLLMHandler", (_LLMHandler,), {"status": status})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> str:
        self.thread.start()
        host, port = self.server.server_address
        return f"http://{host}:{port}/v1/chat/completions"

    def __exit__(self, *_: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture(scope="module")
def stack(request: pytest.FixtureRequest) -> Iterator[DemoStack]:
    stack = start_stack()
    try:
        yield stack
    finally:
        # 若本模块内任一用例在 call 阶段失败，保留现场并打印子进程日志（B-03）。
        failed = bool(getattr(request.session, "_e2e_call_failed", False))
        stop_stack(stack, failed=failed)


@pytest.fixture
def isolated_stack(request: pytest.FixtureRequest) -> Iterator[DemoStack]:
    stack = start_stack()
    try:
        yield stack
    finally:
        failed = bool(getattr(request.node, "rep_call", None) and request.node.rep_call.failed)
        stop_stack(stack, failed=failed)


def _component(client: httpx.Client, name: str) -> dict:
    components = client.get("/components").json()
    match = next((item for item in components if item["name"] == name), None)
    assert match is not None, f"未找到组件 {name}"
    return match


def _run_component(client: httpx.Client, name: str, payload: dict) -> dict:
    component = _component(client, name)
    created = client.post(f"/components/{component['id']}/runs", json={"input": payload})
    assert created.status_code == 202, created.text
    run_id = created.json()["id"]
    return poll_run(client, run_id)


def _events(client: httpx.Client, run_id: str) -> set[str]:
    events = client.get(f"/runs/{run_id}/events").json()
    return {item["type"] for item in events}


def _workflow_session(client: httpx.Client) -> str:
    created = client.post("/workflow-tools/sessions", json={})
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _publish_workflow(
    client: httpx.Client, workflow_id: str
) -> httpx.Response:
    workflow = client.get(f"/workflows/{workflow_id}")
    assert workflow.status_code == 200, workflow.text
    snapshot = workflow.json()
    metadata = snapshot.get("draftMetadata") or {}
    return client.post(
        f"/workflows/{workflow_id}/publish",
        json={
            "draftRevision": snapshot["draftRevision"],
            "draftDigest": snapshot["draftDigest"],
            "replayEvidenceId": metadata.get("replayEvidenceId"),
        },
    )


def test_e2e_1_deterministic_component(stack: DemoStack) -> None:
    with stack.client() as client:
        run = _run_component(client, "确定性问候", {"name": "Alice"})
        assert run["status"] == "succeeded"
        assert run["output"] == {"message": "你好，Alice"}
        types = _events(client, run["id"])
        assert {"run.created", "run.completed"} <= types
        # 无模型配置：不应出现任何 LLM/Agent Token 事件。
        assert not {"llm.call.completed", "agent.completed"} & types


def test_e2e_2_mcp_component(stack: DemoStack) -> None:
    with stack.client() as client:
        run = _run_component(client, "本地天气查询", {"city": "成都"})
        assert run["status"] == "succeeded"
        assert run["output"]["city"] == "成都"
        types = _events(client, run["id"])
        assert "mcp.call.completed" in types
        # 锁定 MCP ToolSnapshot，模型调用为 0。
        assert "llm.call.completed" not in types


def test_e2e_3_hybrid_component(stack: DemoStack) -> None:
    with stack.client() as client:
        run = _run_component(client, "反馈分类", {"text": "产品很好"})
        assert run["status"] == "succeeded"
        assert run["output"] == {"category": "feedback"}
        assert "llm.call.completed" in _events(client, run["id"])


def test_e2e_3b_agent_structured_fallback_for_node_and_presentation(
    isolated_stack: DemoStack,
) -> None:
    """真实 API/Worker/Agent 子进程：LLM 未配置时两类结构化生成共用冻结 fallback。"""

    with isolated_stack.client() as client:
        component = _component(client, "反馈分类")
        workflow = client.get(f"/workflows/{component['workflowId']}").json()
        definition = workflow["definition"]
        semantic = next(
            node for node in definition["spec"]["nodes"] if node["type"] == "llm"
        )
        semantic["config"].pop("modelProfile", None)
        definition["metadata"]["presentationPolicy"] = {
            "enabled": True,
            "audience": "end_user",
            "producer": "llm",
            "prompt": "用一句话说明分类结果",
            "language": "zh-CN",
            "maxLength": 120,
        }
        updated = client.put(
            f"/workflows/{component['workflowId']}/draft",
            json={"revision": workflow["draftRevision"], "definition": definition},
        )
        assert updated.status_code == 200, updated.text
        published = _publish_workflow(client, component["workflowId"])
        assert published.status_code == 201, published.text
        created = client.post(
            f"/workflow-versions/{published.json()['id']}/runs",
            json={"input": {"text": "这个产品很好"}},
        )
        assert created.status_code == 202, created.text
        run = poll_run(client, created.json()["id"], timeout=60)
        assert run["status"] == "succeeded", run

        deadline = time.time() + 30
        detail = run
        while time.time() < deadline:
            detail = client.get(f"/runs/{run['id']}").json()
            if (detail.get("presentation") or {}).get("status") == "completed":
                break
            time.sleep(0.2)
        assert detail["runtimeSnapshot"]["runtimeUsed"] == "agent_fallback"
        assert detail["runtimeSnapshot"]["fallbackReason"] == "LLM_RUNTIME_UNCONFIGURED"
        assert detail["presentation"]["status"] == "completed"
        assert len(detail["modelUsage"]) == 2
        assert {
            item["runtimeUsed"] for item in detail["modelUsage"]
        } == {"agent_fallback"}
        assert all(item["complete"] for item in detail["modelUsage"])
        assert all(item["totalTokens"] == 20 for item in detail["modelUsage"])
        llm_node = next(node for node in detail["nodes"] if node["type"] == "llm")
        assert llm_node["metrics"]["toolRequestCount"] == 0
        assert "tool.requested" not in _events(client, run["id"])


@pytest.mark.parametrize(
    ("provider_status", "expected_code"),
    [(200, None), (401, "MODEL_AUTH_FAILED")],
)
def test_e2e_3c_direct_llm_does_not_fallback_after_request_started(
    request: pytest.FixtureRequest,
    provider_status: int,
    expected_code: str | None,
) -> None:
    with _LLMServer(provider_status) as endpoint:
        direct_stack = start_stack(
            model_env={
                "SKILLX_LLM_RUNTIME_TYPE": "openai_compatible",
                "SKILLX_LLM_ENDPOINT": endpoint,
                "SKILLX_LLM_MODEL": "e2e-direct-model",
                "SKILLX_LLM_API_KEY_ENV": "SKILLX_E2E_LLM_KEY",
                "SKILLX_E2E_LLM_KEY": "configured",
            }
        )
        failed = True
        try:
            with direct_stack.client() as client:
                component = _component(client, "反馈分类")
                workflow = client.get(f"/workflows/{component['workflowId']}").json()
                definition = workflow["definition"]
                semantic = next(
                    node for node in definition["spec"]["nodes"] if node["type"] == "llm"
                )
                semantic["config"].pop("modelProfile", None)
                updated = client.put(
                    f"/workflows/{component['workflowId']}/draft",
                    json={
                        "revision": workflow["draftRevision"],
                        "definition": definition,
                    },
                )
                assert updated.status_code == 200, updated.text
                published = _publish_workflow(client, component["workflowId"])
                assert published.status_code == 201, published.text
                created = client.post(
                    f"/workflow-versions/{published.json()['id']}/runs",
                    json={"input": {"text": "这个产品很好"}},
                )
                run = poll_run(client, created.json()["id"], timeout=60)
                detail = client.get(f"/runs/{run['id']}").json()
                assert detail["runtimeSnapshot"]["runtimeUsed"] == "direct_llm"
                assert len(detail["modelUsage"]) == 1
                usage = detail["modelUsage"][0]
                assert usage["runtimeUsed"] == "direct_llm"
                assert usage["fallbackReason"] is None
                assert "tool.requested" not in _events(client, run["id"])
                if expected_code is None:
                    assert run["status"] == "succeeded", run
                    assert usage["complete"] is True
                    assert usage["totalTokens"] == 10
                else:
                    assert run["status"] == "failed", run
                    assert run["error"]["code"] == expected_code
                    assert usage["complete"] is False
                    assert usage["totalTokens"] is None
                failed = False
        finally:
            failed = failed or bool(
                getattr(request.node, "rep_call", None)
                and request.node.rep_call.failed
            )
            stop_stack(direct_stack, failed=failed)


def test_e2e_4_conversation_component(stack: DemoStack) -> None:
    with stack.client() as client:
        conversation = client.post("/conversations", json={"title": "验收对话"}).json()
        message = client.post(
            f"/conversations/{conversation['id']}/messages",
            json={"content": "生成问候语 greeting 欢迎 welcome"},
        )
        assert message.status_code == 201, message.text
        pending = message.json()["assistant"]
        assert pending["state"] == "ask_user"
        assert pending["agentAction"] == "ask_user"
        assert pending["pendingSelectionId"]
        assert pending["refs"][0].startswith("workflow:")
        assert {item["level"] for item in pending["toolTimeline"]} == {"L0", "L1"}

        resumed = client.post(
            f"/conversations/{conversation['id']}/messages",
            json={"content": "Bob"},
        )
        assert resumed.status_code == 201, resumed.text
        selected = resumed.json()["assistant"]
        assert selected["state"] == "workflow_run"
        assert selected["resumedPendingSelection"] is True
        run = poll_run(client, selected["runId"])
        assert run["status"] == "succeeded"
        assert run["output"] == {"message": "你好，Bob"}


def test_e2e_5_agent_distillation_reuse(stack: DemoStack) -> None:
    with stack.client() as client:
        # 1) 通过 HTTP 创建对话并启动持久化长任务 Agent（先持久化，后执行）。
        conversation = client.post("/conversations", json={"title": "Agent 固化"}).json()
        started = client.post(
            f"/conversations/{conversation['id']}/agent-runs",
            json={
                "content": "q1 [[MODE:deterministic]]",
                "allowedCapabilities": ["lookup.table"],
            },
        )
        # POST 立即返回 202 + queued handle，不阻塞到 TraeX 终态。
        assert started.status_code == 202, started.text
        payload = started.json()
        assert payload["status"] == "queued"
        conversation_run_id = payload["conversationRunId"]

        # 2) 独立 Agent Worker 领取租约并执行，运行事件跨连接可见、可恢复。
        summary = poll_conversation_run(client, conversation_run_id)
        assert summary["status"] == "succeeded", summary
        assert summary["runType"] == "agent_run"
        assert summary["output"] == {"reply": "第一季度：营收增长 12%"}
        events = client.get(f"/conversation-runs/{conversation_run_id}/events").json()
        event_types = {item["type"] for item in events}
        assert {
            "agent.run.created",
            "agent.run.started",
            "agent.plan",
            "agent.run.succeeded",
        } <= event_types
        assert any(item["type"].startswith("tool.") for item in events)

        # 3) 固化消费真实 Tool 轨迹，提炼出确定节点（非单 Agent 节点），只生成草稿。
        distilled = client.post(f"/conversation-runs/{conversation_run_id}/distill")
        assert distilled.status_code == 201, distilled.text
        draft = distilled.json()
        draft_workflow_id = draft["draftWorkflowId"]
        assert draft["status"] == "draft_created"
        assert draft["analysis"]["sourceRunType"] == "agent_run"
        # 提炼出确定节点，且固化后预计模型调用降为 0。
        assert draft["analysis"]["directNodes"] >= 1
        assert draft["analysis"]["agentNodes"] == 0
        assert draft["analysis"]["estimatedLlmCallsAfter"] == 0

        # 4) 通过 API 人工发布；第二次复用不得创建 Component 后持 id 直调。
        published = _publish_workflow(client, draft_workflow_id)
        assert published.status_code == 201, published.text
        version_id = published.json()["id"]

        # 5) 新 Conversation 以自然语言和不同业务键发现固化版本并交给 Runtime。
        second_conversation = client.post(
            "/conversations", json={"title": "非网页新对话复用"}
        ).json()
        selected = client.post(
            f"/conversations/{second_conversation['id']}/messages",
            json={"content": "使用业务键 q2 执行结构化查询"},
        )
        assert selected.status_code == 201, selected.text
        assistant = selected.json()["assistant"]
        assert assistant["state"] == "workflow_run"
        assert assistant["workflowVersionId"] == version_id
        assert {item["level"] for item in assistant["toolTimeline"]} == {"L0", "L1"}
        run = poll_run(client, assistant["runId"])
        assert run["status"] == "succeeded", run
        # 新输入 q2 选出不同结果（编译复用，而非重跑同一 prompt）。
        assert run["output"] == {"reply": "第二季度：营收增长 8%"}
        reuse_events = _events(client, run["id"])
        assert "agent.completed" not in reuse_events
        assert "llm.call.completed" not in reuse_events


def test_natural_message_entry_runs_read_only_browser_lookup_and_file(
    stack: DemoStack,
) -> None:
    stack.set_article("自然入口网页正文")
    tasks = [
        (
            (
                f"{stack.form_url}/article "
                "[[SELECTION:continue_agent]][[MODE:browser_summary]]"
            ),
            "自然入口网页正文",
            "browser.read",
        ),
        (
            "q2 [[SELECTION:continue_agent]][[MODE:deterministic]]",
            "第二季度：营收增长 8%",
            "lookup.table",
        ),
        (
            (
                "读取 source.txt [[SELECTION:continue_agent]]"
                "[[MODE:local_summary]][[FILE:source.txt]]"
                "[[CONTENT:自然入口文件正文]]"
            ),
            "自然入口文件正文",
            "read.local",
        ),
    ]
    with stack.client() as client:
        for prompt, expected, capability in tasks:
            conversation = client.post(
                "/conversations", json={"title": f"自然入口-{capability}"}
            ).json()
            sent = client.post(
                f"/conversations/{conversation['id']}/messages",
                json={"content": prompt},
            )
            assert sent.status_code == 201, sent.text
            assistant = sent.json()["assistant"]
            assert assistant["type"] == "agent_run"
            assert assistant["runType"] == "agent_run"
            assert assistant["state"] == "queued"

            summary = poll_conversation_run(
                client, assistant["conversationRunId"], timeout=60
            )
            assert summary["status"] == "succeeded", summary
            assert summary["output"] == {"reply": expected}
            events = client.get(
                f"/conversation-runs/{assistant['conversationRunId']}/events"
            ).json()
            created = next(
                item for item in events if item["type"] == "agent.run.created"
            )
            assert capability in created["payload"]["allowedCapabilities"]


def test_e2e_6_browser_submit_with_one_time_approval(stack: DemoStack) -> None:
    with stack.client() as client:
        component = _component(client, "费用提交（真实浏览器）")
        created = client.post(
            f"/components/{component['id']}/runs",
            json={"input": {"amount": 100}},
        )
        assert created.status_code == 202, created.text
        run_id = created.json()["id"]
        run = poll_run(client, run_id)
        # 第一次执行：真实 Playwright 检查/填写后等待一次性提交授权。
        assert run["status"] == "waiting_approval", run

        # 尚未提交，表单站点提交计数为 0。
        assert stack.form_stats()["submitCount"] == 0

        approvals = client.get(f"/runs/{run_id}").json()["approvals"]
        pending = next(item for item in approvals if item["status"] == "pending")
        approved = client.post(
            f"/approvals/{pending['id']}/approve",
            json={"snapshotDigest": pending["snapshotDigest"]},
        )
        assert approved.status_code == 200, approved.text

        # 批准后重放、提交并成功断言。
        run = poll_run(client, run_id)
        assert run["status"] == "succeeded", run
        assert run["output"]["businessId"] == "EXP-DEMO-001"
        assert "browser.submit.completed" in _events(client, run_id)

        # HTTP server 只收到一次提交。
        stats = stack.form_stats()
        assert stats["submitCount"] == 1, stats
        assert stats["lastSubmission"] == {"amount": "100"}


def test_e2e_6b_page_change_after_approval_invalidates_authorization(stack: DemoStack) -> None:
    with stack.client() as client:
        component = _component(client, "费用提交（真实浏览器）")
        created = client.post(
            f"/components/{component['id']}/runs",
            json={"input": {"amount": 200}},
        )
        assert created.status_code == 202, created.text
        run_id = created.json()["id"]
        run = poll_run(client, run_id)
        assert run["status"] == "waiting_approval", run

        approvals = client.get(f"/runs/{run_id}").json()["approvals"]
        pending = next(item for item in approvals if item["status"] == "pending")

        # 审批前页面结构变化：新增必填字段，触发 L3 漂移，旧授权失效。
        httpx.get(f"{stack.form_url}/__mutate", timeout=5)
        approved = client.post(
            f"/approvals/{pending['id']}/approve",
            json={"snapshotDigest": pending["snapshotDigest"]},
        )
        assert approved.status_code == 200, approved.text

        run = poll_run(client, run_id)
        # 页面结构性变化使重放失败，不得静默提交。
        assert run["status"] == "failed", run
        # 未产生任何提交（本用例的 amount=200 从未落库）。
        submissions = stack.form_stats()["lastSubmission"]
        assert submissions != {"amount": "200"}


def test_e2e_7_checkpoint_recovery_resumes_without_replaying_side_effects(
    stack: DemoStack,
) -> None:
    with stack.client() as client:
        component = _component(client, "检查点接力演示")
        created = client.post(
            f"/components/{component['id']}/runs",
            json={"input": {"seed": "abc"}},
        )
        assert created.status_code == 202, created.text
        run_id = created.json()["id"]
        # risky 节点重试耗尽 → checkpoint → 恢复 Agent 从剩余子图接力 → 成功。
        run = poll_run(client, run_id, timeout=60)
        assert run["status"] == "succeeded", run
        types = _events(client, run_id)
        # 出现了恢复请求事件。
        assert "run.recovery_requested" in types
        detail = client.get(f"/runs/{run_id}").json()
        nodes = detail["nodes"]
        # sensitive（非幂等副作用）只执行一次，未被重放。
        sensitive_runs = [n for n in nodes if n["nodeId"] == "sensitive"]
        assert len(sensitive_runs) == 1
        assert sensitive_runs[0]["status"] == "succeeded"
        # 原失败 risky NodeRun 保持不可变（failed），恢复通过 substitution 续跑。
        risky_runs = [n for n in nodes if n["nodeId"] == "risky"]
        assert all(n["status"] == "failed" for n in risky_runs)
        # checkpoint 证据暴露唯一 Recovery NodeRun 与正确 fallbackTarget。
        checkpoint = detail["checkpoints"][0]
        assert checkpoint["fallbackTarget"] == "recovery_agent"
        assert checkpoint["recoveryNodeRunId"]


def test_e2e_7c_recovery_request_user_answers_same_checkpoint(
    stack: DemoStack,
) -> None:
    with stack.client() as client:
        component = _component(client, "检查点接力演示")
        created = client.post(
            f"/components/{component['id']}/runs",
            json={"input": {"seed": "request-user"}},
        )
        waiting = poll_run(client, created.json()["id"], timeout=60)
        assert waiting["status"] == "waiting_approval", waiting
        approval = next(
            item
            for item in waiting["approvals"]
            if item["snapshot"].get("effect") == "recovery.question"
        )
        answered = client.post(
            f"/approvals/{approval['id']}/answer",
            json={
                "snapshotDigest": approval["snapshotDigest"],
                "answer": {"confirmed": True},
            },
        )
        assert answered.status_code == 200, answered.text
        completed = poll_run(client, waiting["id"], timeout=60)
        assert completed["status"] == "succeeded", completed
        checkpoints = client.get(f"/runs/{waiting['id']}").json()["checkpoints"]
        assert len(checkpoints) == 1
        assert checkpoints[0]["status"] == "applied"


def test_e2e_7d_recovery_typed_replan_executes_new_attempt(
    stack: DemoStack,
) -> None:
    with stack.client() as client:
        component = _component(client, "检查点接力演示")
        created = client.post(
            f"/components/{component['id']}/runs",
            json={"input": {"seed": "retry-replanned"}},
        )
        completed = poll_run(client, created.json()["id"], timeout=60)
        assert completed["status"] == "succeeded", completed
        detail = client.get(f"/runs/{completed['id']}").json()
        risky = [node for node in detail["nodes"] if node["nodeId"] == "risky"]
        assert len(risky) >= 2
        assert all(node["status"] == "failed" for node in risky[:-1])
        assert risky[-1]["status"] == "succeeded"
        assert detail["checkpoints"][0]["decision"]["action"] == "retry_replanned"


def test_e2e_8_dynamic_web_summary_reflects_page_change(stack: DemoStack) -> None:
    """F-01：真实读取动态网页正文并交给 TraeX 总结；页面改变后第二次输出依赖新正文。"""

    with stack.client() as client:
        component = _component(client, "动态网页总结")
        # 第一次：读取初始正文并总结。
        stack.set_article("第一版正文：季度营收增长 12%。")
        first = _run_component(
            client, "动态网页总结", {"url": f"{stack.form_url}/article"}
        )
        assert first["status"] == "succeeded", first
        assert "12%" in first["output"]["reply"]
        # 确认真实读取事件（非常量正文）。
        assert "browser.read.completed" in _events(client, first["id"])

        # 修改页面正文后第二次运行：必须输出新正文，禁止使用首次结果/常量。
        stack.set_article("第二版正文：季度营收下滑 5%。")
        created = client.post(
            f"/components/{component['id']}/runs",
            json={"input": {"url": f"{stack.form_url}/article"}},
        )
        assert created.status_code == 202, created.text
        second = poll_run(client, created.json()["id"], timeout=60)
        assert second["status"] == "succeeded", second
        assert "下滑 5%" in second["output"]["reply"]
        assert "12%" not in second["output"]["reply"]


def test_e2e_9_reproducible_failure_gated_patch(stack: DemoStack) -> None:
    """公开 Browser 故障链：瞬态拒绝，契约漂移经 Agent proposal 发布同源 v2。"""

    with stack.client() as client:
        transient_definition = {
            "apiVersion": "workflow.platform/v1",
            "kind": "Workflow",
            "metadata": {"name": "transient-control"},
            "spec": {
                "inputSchema": {
                    "type": "object",
                    "required": ["url"],
                    "properties": {"url": {"type": "string"}},
                },
                "outputSchema": {
                    "type": "object",
                    "required": ["content"],
                    "properties": {"content": {"type": "string"}},
                },
                "nodes": [
                    {"id": "input", "type": "input", "displayName": "输入"},
                    {
                        "id": "work",
                        "type": "browser",
                        "displayName": "真实 Browser 瞬态对照",
                        "input": {"url": "${input.url}"},
                        "timeoutSeconds": 10,
                        "config": {
                            "action": "read",
                            "runtime": "playwright",
                            "pageContract": {
                                "contractVersion": "1",
                                "urlPattern": f"{stack.form_url}/transient*",
                                "bodySelector": "#main",
                                "fields": {},
                                "outputSchema": {
                                    "type": "object",
                                    "required": ["content"],
                                    "properties": {
                                        "content": {"type": "string"}
                                    },
                                },
                                "timeoutMs": 1000,
                                "retryable": True,
                            },
                        },
                    },
                    {
                        "id": "output",
                        "type": "output",
                        "displayName": "输出",
                        "input": {"content": "${work.output.content}"},
                    },
                ],
                "edges": [
                    {"source": "input", "target": "work"},
                    {"source": "work", "target": "output"},
                ],
            },
        }
        transient_workflow = client.post(
            "/workflows",
            json={
                "name": "Transient Control",
                "definition": transient_definition,
            },
        ).json()
        transient_v1 = _publish_workflow(
            client, transient_workflow["id"]
        ).json()
        transient_component = client.post(
            "/components",
            json={
                "name": "Transient Control",
                "workflowVersionId": transient_v1["id"],
            },
        ).json()
        observations = {}
        for mode in ("503", "disconnect"):
            for _ in range(5):
                transient_created = client.post(
                    f"/components/{transient_component['id']}/runs",
                    json={
                        "input": {
                            "url": f"{stack.form_url}/transient?mode={mode}"
                        }
                    },
                )
                transient_run = poll_run(
                    client, transient_created.json()["id"]
                )
                assert transient_run["status"] == "failed"
                observation = transient_run["failureObservations"][0]
                assert observation["evidenceKind"] == "transport_error"
                observations[observation["evidenceKind"]] = observation
        assert observations["transport_error"]["occurrence"] == 10
        timeout_definition = deepcopy(transient_definition)
        timeout_definition["metadata"]["name"] = "timeout-control"
        timeout_definition["spec"]["nodes"][1]["timeoutSeconds"] = 1
        timeout_workflow = client.post(
            "/workflows",
            json={
                "name": "Timeout Control",
                "definition": timeout_definition,
            },
        ).json()
        timeout_v1 = _publish_workflow(
            client, timeout_workflow["id"]
        ).json()
        timeout_component = client.post(
            "/components",
            json={
                "name": "Timeout Control",
                "workflowVersionId": timeout_v1["id"],
            },
        ).json()
        for _ in range(5):
            timeout_created = client.post(
                f"/components/{timeout_component['id']}/runs",
                json={
                    "input": {
                        "url": f"{stack.form_url}/transient?mode=timeout"
                    }
                },
            )
            timeout_run = poll_run(client, timeout_created.json()["id"])
            assert timeout_run["status"] == "failed"
            timeout_observation = timeout_run["failureObservations"][0]
            assert timeout_observation["evidenceKind"] == "timeout"
            observations[timeout_observation["evidenceKind"]] = (
                timeout_observation
            )
        assert observations["timeout"]["occurrence"] == 5
        for observation in observations.values():
            assert observation["classification"] == "transient"
            transient_proposal = client.post(
                f"/failure-observations/{observation['id']}/revision-proposal"
            )
            assert transient_proposal.status_code == 422
            assert (
                transient_proposal.json()["detail"]["code"]
                == "REVISION_NOT_ELIGIBLE"
            )
        recovered = client.post(
            f"/components/{transient_component['id']}/runs",
            json={
                "input": {
                    "url": f"{stack.form_url}/transient?mode=ok"
                }
            },
        )
        assert poll_run(client, recovered.json()["id"])["status"] == "succeeded"
        unchanged = client.get(
            f"/workflows/{transient_workflow['id']}"
        ).json()
        assert unchanged["draftMetadata"]["status"] == "published"

        browser_definition = {
            "apiVersion": "workflow.platform/v1",
            "kind": "Workflow",
            "metadata": {"name": "revision-browser"},
            "spec": {
                "inputSchema": {
                    "type": "object",
                    "required": ["url"],
                    "properties": {"url": {"type": "string"}},
                },
                "outputSchema": {
                    "type": "object",
                    "required": ["content"],
                    "properties": {"content": {}},
                },
                "nodes": [
                    {"id": "input", "type": "input", "displayName": "输入"},
                    {
                        "id": "read",
                        "type": "browser",
                        "displayName": "外部读取",
                        "input": {"url": "${input.url}"},
                        "config": {
                            "action": "read",
                            "runtime": "playwright",
                            "pageContract": {
                                "contractVersion": "1",
                                "urlPattern": f"{stack.form_url}/article*",
                                "bodySelector": "#main",
                                "fields": {},
                                "outputSchema": {
                                    "type": "object",
                                    "required": ["content"],
                                    "properties": {
                                        "content": {"type": "integer"}
                                    },
                                },
                            },
                        },
                    },
                    {
                        "id": "output",
                        "type": "output",
                        "displayName": "输出",
                        "input": {"content": "${read.output.content}"},
                    },
                ],
                "edges": [
                    {"source": "input", "target": "read"},
                    {"source": "read", "target": "output"},
                ],
            },
        }
        workflow = client.post(
            "/workflows",
            json={
                "name": "Revision Browser",
                "definition": browser_definition,
            },
        ).json()
        published_v1 = _publish_workflow(client, workflow["id"]).json()
        browser_component = client.post(
            "/components",
            json={
                "name": "Revision Browser v1",
                "workflowVersionId": published_v1["id"],
            },
        ).json()
        source_run_id = None
        failed = None
        for index in range(2):
            created = client.post(
                f"/components/{browser_component['id']}/runs",
                json={
                    "input": {
                        "url": f"{stack.form_url}/article?failure={index}"
                    }
                },
            )
            failed = poll_run(client, created.json()["id"])
            assert failed["status"] == "failed", failed
            source_run_id = failed["id"]
        observation = failed["failureObservations"][0]
        assert observation["occurrence"] == 2
        assert observation["evidenceKind"] == "observed_contract_output"
        proposal_response = client.post(
            f"/failure-observations/{observation['id']}/revision-proposal"
        )
        assert proposal_response.status_code == 200, proposal_response.text
        proposal = proposal_response.json()
        patched = client.post(
            f"/workflow-versions/{published_v1['id']}/workflow-patch",
            json={
                "sourceRunId": source_run_id,
                "proposalId": proposal["proposalId"],
                "proposalDigest": proposal["proposalDigest"],
                "patch": proposal["proposal"],
            },
        )
        assert patched.status_code == 201, patched.text
        proof = patched.json()
        assert proof["draftWorkflowId"] == workflow["id"]
        assert proof["expectedNextVersion"] == 2
        assert proof["replay"]["baselineReproduced"] is True
        assert proof["replay"]["passed"] is True
        assert proof["replay"]["replayEvidenceId"]
        edited = proof["definition"]
        edited_read = next(
            node
            for node in edited["spec"]["nodes"]
            if node["id"] == "read"
        )
        edited_read["timeoutSeconds"] = 45
        dirty_response = client.put(
            f"/workflows/{workflow['id']}/draft",
            json={
                "revision": proof["draftRevision"],
                "definition": edited,
            },
        )
        assert dirty_response.status_code == 200, dirty_response.text
        dirty = dirty_response.json()
        assert dirty["draftMetadata"]["status"] == "dirty_after_replay"
        old_snapshot_publish = client.post(
            f"/workflows/{workflow['id']}/publish",
            json={
                "draftRevision": proof["draftRevision"],
                "draftDigest": proof["draftDigest"],
                "replayEvidenceId": proof["replay"]["replayEvidenceId"],
            },
        )
        assert old_snapshot_publish.status_code == 409
        assert (
            old_snapshot_publish.json()["detail"]["code"]
            == "DRAFT_SNAPSHOT_CONFLICT"
        )
        stale_proof_publish = client.post(
            f"/workflows/{workflow['id']}/publish",
            json={
                "draftRevision": dirty["draftRevision"],
                "draftDigest": dirty["draftDigest"],
                "replayEvidenceId": proof["replay"]["replayEvidenceId"],
            },
        )
        assert stale_proof_publish.status_code == 409
        assert (
            stale_proof_publish.json()["detail"]["code"]
            == "REPLAY_PROOF_STALE"
        )
        reapproved_response = client.post(
            f"/workflows/{workflow['id']}/replay"
        )
        assert reapproved_response.status_code == 201, reapproved_response.text
        reapproved = reapproved_response.json()
        assert (
            reapproved["replay"]["replayEvidenceId"]
            != proof["replay"]["replayEvidenceId"]
        )
        published_v2 = _publish_workflow(client, workflow["id"]).json()
        assert published_v2["workflowId"] == workflow["id"]
        assert published_v2["version"] == 2

        def run_discovered(url: str) -> dict:
            session_id = _workflow_session(client)
            searched = client.post(
                "/workflow-tools/search",
                json={
                    "sessionId": session_id,
                    "query": "Revision Browser",
                },
            ).json()
            refs = [
                item["workflowRef"]
                for item in searched["items"]
                if item["name"] == "Revision Browser"
            ]
            assert refs == [published_v2["workflowRef"]]
            described = client.post(
                "/workflow-tools/describe",
                json={"sessionId": session_id, "ref": refs[0]},
            )
            assert described.status_code == 200, described.text
            selected = client.post(
                "/workflow-tools/execute-selection",
                json={
                    "action": "run_full",
                    "reason": "E2E revised reuse",
                    "refs": refs,
                    "bindings": {"url": url},
                    "missingInputs": [],
                    "sessionId": session_id,
                },
            )
            assert selected.status_code == 202, selected.text
            return poll_run(client, selected.json()["runId"])

        stack.set_article("修订后原输入正文")
        original = run_discovered(f"{stack.form_url}/article?version=a")
        stack.set_article("修订后新输入正文")
        revised = run_discovered(f"{stack.form_url}/article?version=b")
        assert original["output"] == {"content": "修订后原输入正文"}
        assert revised["output"] == {"content": "修订后新输入正文"}
        assert revised["dependencyVersions"]["revisionLineage"][
            "patchOfObservationId"
        ] == observation["id"]

        old_session = _workflow_session(client)
        old_resolved = client.post(
            "/workflow-tools/resolve",
            json={
                "sessionId": old_session,
                "ref": published_v1["workflowRef"],
            },
        )
        assert old_resolved.status_code == 200, old_resolved.text
        assert old_resolved.json()["workflow"]["versionLock"][
            "versionId"
        ] == published_v1["id"]


def test_agent_run_handoff_public_api_and_completed_action_ledger(
    stack: DemoStack,
) -> None:
    restart_worker(
        stack,
        crash=True,
        disable_task_recovery=True,
    )
    with stack.client() as client:
        conversation = client.post(
            "/conversations", json={"title": "公开接力"}
        ).json()
        created = client.post(
            f"/conversations/{conversation['id']}/agent-runs",
            json={
                "content": (
                    "q1 [[MODE:deterministic_fail_after_tool]]"
                    "[[RECOVERY_REPEAT]]"
                ),
                "allowedCapabilities": ["lookup.table"],
            },
        )
        assert created.status_code == 202, created.text
        source_id = created.json()["conversationRunId"]
        source = poll_conversation_run(client, source_id)
        assert source["status"] == "failed"
        checkpoint_id = source["error"]["taskCheckpointId"]
        pending = client.get(
            f"/task-checkpoints/{checkpoint_id}"
        ).json()
        assert pending["status"] == "pending"
        assert pending["claimGeneration"] == 0
        assert pending["continuationRunId"] is None
        assert pending["completedActionLedger"]

        restart_worker(stack, crash=True)
        deadline = time.time() + 30
        checkpoint = {}
        while time.time() < deadline:
            response = client.get(f"/task-checkpoints/{checkpoint_id}")
            assert response.status_code == 200, response.text
            checkpoint = response.json()
            if checkpoint.get("continuationRunId"):
                break
            time.sleep(0.1)
        continuation_id = checkpoint["continuationRunId"]
        assert checkpoint["claimGeneration"] == 1
        assert checkpoint["completedActionLedger"]
        continuation = poll_conversation_run(client, continuation_id)
        assert continuation["status"] == "succeeded"
        assert continuation["output"] == {
            "reply": "第一季度：营收增长 12%"
        }
        events = client.get(
            f"/conversation-runs/{continuation_id}/events"
        ).json()
        repeated_completed = [
            item
            for item in events
            if item["type"] == "tool.requested"
            and item["payload"].get("planStepId") == "lookup"
        ]
        assert repeated_completed == []

        deadline = time.time() + 30
        while time.time() < deadline:
            checkpoint = client.get(
                f"/task-checkpoints/{checkpoint_id}"
            ).json()
            if checkpoint["status"] == "succeeded":
                break
            time.sleep(0.1)
        assert checkpoint["status"] == "succeeded"
        assert poll_conversation_run(client, source_id)["status"] == "failed"


def test_real_tool_failure_handoff_distill_and_second_reuse(
    isolated_stack: DemoStack,
) -> None:
    stack = isolated_stack
    restart_worker(stack, crash=True, disable_task_recovery=True)
    with stack.client() as client:
        stack.set_transient_healthy(False)
        transient_url = f"{stack.form_url}/transient?mode=503"
        conversation = client.post(
            "/conversations", json={"title": "真实失败接力"}
        ).json()
        created = client.post(
            f"/conversations/{conversation['id']}/agent-runs",
            json={
                "content": (
                    f"{transient_url} [[MODE:planned_browser_summary]]"
                ),
                "allowedCapabilities": [
                    "lookup.table",
                    "browser.read",
                    "summarize.text",
                ],
            },
        )
        assert created.status_code == 202, created.text
        source_id = created.json()["conversationRunId"]
        source = poll_conversation_run(client, source_id)
        assert source["status"] == "failed"
        assert source["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
        assert source["error"]["code"] != "AGENT_EVENT_INVALID"
        checkpoint_id = source["error"]["taskCheckpointId"]
        checkpoint = client.get(
            f"/task-checkpoints/{checkpoint_id}"
        ).json()
        assert checkpoint["status"] == "pending"
        assert checkpoint["failedAction"]["callId"] == "call-browser"
        assert checkpoint["completedActionLedger"]
        assert stack.form_stats()["transientCount"] == 2

        stack.set_transient_healthy(True)
        restart_worker(stack, crash=True)
        deadline = time.time() + 30
        continuation_id = None
        while time.time() < deadline:
            checkpoint = client.get(
                f"/task-checkpoints/{checkpoint_id}"
            ).json()
            continuation_id = checkpoint.get("continuationRunId")
            if continuation_id:
                break
            time.sleep(0.1)
        continuation = poll_conversation_run(
            client, continuation_id, timeout=30
        )
        assert continuation["status"] == "succeeded"
        assert continuation["output"] == {"reply": "恢复后的正文"}
        assert continuation["input"]["continuationOfConversationRunId"] == source_id
        assert continuation["input"]["continuationOfCheckpointId"] == checkpoint_id
        browser_attempts = continuation["stepAttempts"]["browser"]
        assert [item["attempt"] for item in browser_attempts] == [1, 2]
        assert [item["status"] for item in browser_attempts] == [
            "failed",
            "succeeded",
        ]
        assert all(
            item["argumentsDigest"].startswith("sha256:")
            for item in browser_attempts
        )
        assert len(
            {item["argumentsDigest"] for item in browser_attempts}
        ) == 1
        assert stack.form_stats()["transientCount"] == 3
        continuation_events = client.get(
            f"/conversation-runs/{continuation_id}/events"
        ).json()
        assert not any(
            item["type"] == "tool.requested"
            and item["payload"].get("planStepId") == "preflight"
            for item in continuation_events
        )

        distilled = client.post(
            f"/conversation-runs/{continuation_id}/distill"
        )
        assert distilled.status_code == 201, distilled.text
        published = _publish_workflow(
            client, distilled.json()["draftWorkflowId"]
        )
        assert published.status_code == 201, published.text
        version_id = published.json()["id"]

        stack.set_article("接力固化后的新正文")
        second_conversation = client.post(
            "/conversations", json={"title": "失败恢复后复用"}
        ).json()
        selected = client.post(
            f"/conversations/{second_conversation['id']}/messages",
            json={
                "content": (
                    f"访问并总结这个网页：{stack.form_url}/article?handoff=1"
                )
            },
        )
        assert selected.status_code == 201, selected.text
        assistant = selected.json()["assistant"]
        assert assistant["workflowVersionId"] == version_id
        assert {item["level"] for item in assistant["toolTimeline"]} == {
            "L0",
            "L1",
        }
        reused_run = poll_run(client, assistant["runId"], timeout=60)
        assert reused_run["status"] == "succeeded", reused_run
        assert "接力固化后的新正文" in reused_run["output"]["reply"]

        described = client.post(
            "/workflow-tools/describe",
            json={
                "sessionId": assistant["disclosureSessionId"],
                "ref": assistant["refs"][0],
            },
        ).json()["workflow"]
        assert described["lineage"]["sourceConversationRunId"] == continuation_id
        checkpoint = client.get(
            f"/task-checkpoints/{checkpoint_id}"
        ).json()
        assert checkpoint["status"] == "succeeded"
        assert poll_conversation_run(client, source_id)["status"] == "failed"


@pytest.mark.parametrize(
    ("mode", "capabilities", "expected", "completed", "remaining"),
    [
        (
            "planned_lookup_transform",
            ["lookup.table"],
            "第三季度：营收持平",
            ["lookup"],
            ["transform", "output"],
        ),
        (
            "planned_file_transform",
            ["read.local", "summarize.text"],
            "黑盒文件新内容",
            ["read"],
            ["transform"],
        ),
    ],
)
def test_generic_non_browser_failure_continuation(
    isolated_stack: DemoStack,
    mode: str,
    capabilities: list[str],
    expected: str,
    completed: list[str],
    remaining: list[str],
) -> None:
    stack = isolated_stack
    restart_worker(stack, crash=True, disable_task_recovery=True)
    with stack.client() as client:
        conversation = client.post(
            "/conversations", json={"title": f"通用接力-{mode}"}
        ).json()
        created = client.post(
            f"/conversations/{conversation['id']}/agent-runs",
            json={
                "content": (
                    f"任务 [[MODE:{mode}]][[CONTENT:{expected}]]"
                ),
                "allowedCapabilities": capabilities,
            },
        )
        assert created.status_code == 202, created.text
        source = poll_conversation_run(
            client, created.json()["conversationRunId"]
        )
        assert source["status"] == "failed"
        checkpoint_id = source["error"]["taskCheckpointId"]
        checkpoint = client.get(
            f"/task-checkpoints/{checkpoint_id}"
        ).json()
        assert checkpoint["status"] == "pending"
        assert checkpoint["planSnapshot"]["digest"].startswith("sha256:")
        assert [item["id"] for item in checkpoint["completedSteps"]] == completed
        assert [
            item["id"] for item in checkpoint["remainingPlan"]["steps"]
        ] == remaining
        assert checkpoint["reportedError"]["retryable"] is False
        assert checkpoint["effectiveError"]["retryable"] is True

        restart_worker(stack, crash=True)
        deadline = time.time() + 30
        continuation_id = None
        while time.time() < deadline:
            checkpoint = client.get(
                f"/task-checkpoints/{checkpoint_id}"
            ).json()
            continuation_id = checkpoint.get("continuationRunId")
            if continuation_id:
                break
            time.sleep(0.1)
        continuation = poll_conversation_run(
            client, continuation_id, timeout=30
        )
        assert continuation["status"] == "succeeded"
        assert continuation["output"] == {"reply": expected}
        assert continuation["input"]["continuationGeneration"] == 1


@pytest.mark.parametrize(
    ("mode", "directive", "capabilities", "expected_step"),
    [
        (
            "planned_browser_summary",
            "[[RECOVERY_URL:{alternate_url}]]",
            ["lookup.table", "browser.read", "summarize.text"],
            "browser",
        ),
        (
            "planned_file_transform",
            (
                "[[RECOVERY_REPEAT_SUCCEEDED]]"
                "[[RECOVERY_PATH:different.txt]][[CONTENT:safe-content]]"
            ),
            ["read.local", "summarize.text"],
            "read",
        ),
        (
            "planned_lookup_transform",
            "[[RECOVERY_KEY:q4]]",
            ["lookup.table"],
            "transform",
        ),
    ],
)
def test_continuation_argument_deviation_fails_before_adapter(
    isolated_stack: DemoStack,
    mode: str,
    directive: str,
    capabilities: list[str],
    expected_step: str,
) -> None:
    stack = isolated_stack
    restart_worker(stack, crash=True, disable_task_recovery=True)
    with stack.client() as client:
        stack.set_transient_healthy(False)
        alternate_url = f"{stack.form_url}/alternate"
        task_prefix = (
            f"{stack.form_url}/transient?mode=503 "
            if mode == "planned_browser_summary"
            else "任务 "
        )
        content = (
            f"{task_prefix}[[MODE:{mode}]]"
            f"{directive.format(alternate_url=alternate_url)}"
        )
        conversation = client.post(
            "/conversations", json={"title": f"参数偏离-{mode}"}
        ).json()
        created = client.post(
            f"/conversations/{conversation['id']}/agent-runs",
            json={
                "content": content,
                "allowedCapabilities": capabilities,
            },
        )
        source = poll_conversation_run(
            client, created.json()["conversationRunId"]
        )
        assert source["status"] == "failed"
        checkpoint_id = source["error"]["taskCheckpointId"]
        if mode == "planned_browser_summary":
            stack.set_transient_healthy(True)
        restart_worker(stack, crash=True)
        deadline = time.time() + 30
        continuation_id = None
        while time.time() < deadline:
            checkpoint = client.get(
                f"/task-checkpoints/{checkpoint_id}"
            ).json()
            continuation_id = checkpoint.get("continuationRunId")
            if continuation_id:
                break
            time.sleep(0.1)
        continuation = poll_conversation_run(
            client, continuation_id, timeout=30
        )
        assert continuation["status"] == "failed"
        assert continuation["error"]["code"] == "PLAN_ARGUMENT_MISMATCH"
        events = client.get(
            f"/conversation-runs/{continuation_id}/events"
        ).json()
        assert not any(
            item["type"] == "tool.result"
            and item["payload"].get("planStepId") == expected_step
            for item in events
        )
        if mode == "planned_browser_summary":
            assert stack.form_stats()["alternateCount"] == 0


@pytest.mark.parametrize(
    ("directive", "expected_code"),
    [
        ("[[RECOVERY_NEW_PLAN]]", "AGENT_EVENT_INVALID"),
        ("[[RECOVERY_SKIP_DEPENDENCY]]", "AGENT_EVENT_INVALID"),
        ("[[RECOVERY_REPEAT_SUCCEEDED]]", "PLAN_STEP_ALREADY_SUCCEEDED"),
    ],
)
def test_continuation_plan_or_step_deviation_fails_closed(
    isolated_stack: DemoStack,
    directive: str,
    expected_code: str,
) -> None:
    stack = isolated_stack
    restart_worker(stack, crash=True, disable_task_recovery=True)
    with stack.client() as client:
        conversation = client.post(
            "/conversations", json={"title": "计划偏离"}
        ).json()
        created = client.post(
            f"/conversations/{conversation['id']}/agent-runs",
            json={
                "content": (
                    "任务 [[MODE:planned_lookup_transform]]"
                    f"{directive}"
                ),
                "allowedCapabilities": ["lookup.table"],
            },
        )
        source = poll_conversation_run(
            client, created.json()["conversationRunId"]
        )
        checkpoint_id = source["error"]["taskCheckpointId"]
        restart_worker(stack, crash=True)
        deadline = time.time() + 30
        continuation_id = None
        while time.time() < deadline:
            checkpoint = client.get(
                f"/task-checkpoints/{checkpoint_id}"
            ).json()
            continuation_id = checkpoint.get("continuationRunId")
            if continuation_id:
                break
            time.sleep(0.1)
        continuation = poll_conversation_run(
            client, continuation_id, timeout=30
        )
        assert continuation["status"] == "failed"
        assert continuation["error"]["code"] == expected_code


def test_continuation_legal_replan_creates_revision_two_artifact(
    isolated_stack: DemoStack,
) -> None:
    stack = isolated_stack
    restart_worker(stack, crash=True, disable_task_recovery=True)
    with stack.client() as client:
        conversation = client.post(
            "/conversations", json={"title": "合法 replan"}
        ).json()
        created = client.post(
            f"/conversations/{conversation['id']}/agent-runs",
            json={
                "content": (
                    "任务 [[MODE:planned_lookup_transform]]"
                    "[[RECOVERY_REPLAN]][[RECOVERY_REPLAN_REPEAT]]"
                ),
                "allowedCapabilities": ["lookup.table"],
            },
        )
        source = poll_conversation_run(
            client, created.json()["conversationRunId"]
        )
        checkpoint_id = source["error"]["taskCheckpointId"]
        restart_worker(stack, crash=True)
        deadline = time.time() + 30
        continuation_id = None
        while time.time() < deadline:
            checkpoint = client.get(
                f"/task-checkpoints/{checkpoint_id}"
            ).json()
            continuation_id = checkpoint.get("continuationRunId")
            if continuation_id:
                break
            time.sleep(0.1)
        continuation = poll_conversation_run(
            client, continuation_id, timeout=30
        )
        assert continuation["status"] == "succeeded", continuation
        assert (
            continuation["input"]["expectedPlanContract"]["revision"] == 1
        )
        assert continuation["input"]["expectedPlanContract"][
            "remainingRevisions"
        ] == 1
        assert continuation["replanBudget"] == {
            "maxRevisions": 1,
            "usedRevisions": 1,
            "remainingRevisions": 0,
        }
        events = client.get(
            f"/conversation-runs/{continuation_id}/events"
        ).json()
        replans = [
            item for item in events if item["type"] == "agent.replan"
        ]
        assert [item["payload"]["newRevision"] for item in replans] == [2, 2]
        assert replans[1]["payload"]["idempotentReplay"] is True
        assert all(
            item["payload"]["replanBudget"]["usedRevisions"] == 1
            for item in replans
        )
        artifacts = client.get(
            f"/conversation-runs/{continuation_id}/artifacts"
        )
        assert artifacts.status_code == 200, artifacts.text
        assert any(
            item["kind"] == "agent_plan_snapshot"
            and item["metadata"].get("revision") == 2
            for item in artifacts.json()
        )


def test_agent_run_unknown_outcome_answer_consumed_once(
    stack: DemoStack,
) -> None:
    with stack.client() as client:
        conversation = client.post(
            "/conversations", json={"title": "未知结果接力"}
        ).json()
        created = client.post(
            f"/conversations/{conversation['id']}/agent-runs",
            json={"content": "任务 [[MODE:hang]]"},
        )
        assert created.status_code == 202, created.text
        source_id = created.json()["conversationRunId"]
        deadline = time.time() + 20
        while time.time() < deadline:
            source = client.get(
                f"/conversation-runs/{source_id}"
            ).json()
            if source["status"] == "running":
                break
            time.sleep(0.1)
        assert source["status"] == "running"
        restart_worker(stack, crash=True)
        source = poll_conversation_run(client, source_id, timeout=30)
        assert source["status"] == "failed"
        assert source["error"]["code"] == "UNKNOWN_OUTCOME"
        checkpoint_id = source["error"]["taskCheckpointId"]
        checkpoint = client.get(
            f"/task-checkpoints/{checkpoint_id}"
        ).json()
        assert checkpoint["status"] == "awaiting_user"
        answered = client.post(
            f"/task-checkpoints/{checkpoint_id}/answer",
            json={
                "snapshotDigest": checkpoint["digest"],
                "answer": {
                    "continue": True,
                    "externalStateChecked": True,
                },
            },
        )
        assert answered.status_code == 200, answered.text
        duplicate = client.post(
            f"/task-checkpoints/{checkpoint_id}/answer",
            json={
                "snapshotDigest": checkpoint["digest"],
                "answer": {"continue": True},
            },
        )
        assert duplicate.status_code == 409

        deadline = time.time() + 30
        continuation_id = None
        while time.time() < deadline:
            checkpoint = client.get(
                f"/task-checkpoints/{checkpoint_id}"
            ).json()
            continuation_id = checkpoint.get("continuationRunId")
            if continuation_id:
                break
            time.sleep(0.1)
        continuation = poll_conversation_run(
            client, continuation_id, timeout=30
        )
        assert continuation["status"] == "succeeded"


def test_legacy_transient_replan_requires_one_time_user_answer(
    isolated_stack: DemoStack,
) -> None:
    stack = isolated_stack
    restart_worker(stack, crash=True, disable_task_recovery=True)
    with stack.client() as client:
        conversation = client.post(
            "/conversations", json={"title": "legacy replan"}
        ).json()
        created = client.post(
            f"/conversations/{conversation['id']}/agent-runs",
            json={
                "content": (
                    "q1 [[MODE:reported_failure]]"
                    "[[ERROR:DEPENDENCY_UNAVAILABLE]]"
                ),
                "allowedCapabilities": ["lookup.table"],
            },
        )
        source = poll_conversation_run(
            client, created.json()["conversationRunId"]
        )
        checkpoint_id = source["error"]["taskCheckpointId"]
        checkpoint = client.get(
            f"/task-checkpoints/{checkpoint_id}"
        ).json()
        assert checkpoint["status"] == "awaiting_user"
        assert checkpoint["remainingPlan"]["mode"] == "replan_required"
        assert checkpoint["pendingQuestion"]
        assert checkpoint["replanBudget"] == {
            "maxRevisions": 1,
            "usedRevisions": 0,
            "remainingRevisions": 1,
        }
        answer = client.post(
            f"/task-checkpoints/{checkpoint_id}/answer",
            json={
                "snapshotDigest": checkpoint["digest"],
                "answer": {"continue": True},
            },
        )
        assert answer.status_code == 200, answer.text
        duplicate = client.post(
            f"/task-checkpoints/{checkpoint_id}/answer",
            json={
                "snapshotDigest": checkpoint["digest"],
                "answer": {"continue": True},
            },
        )
        assert duplicate.status_code == 409
        restart_worker(stack, crash=True)
        deadline = time.time() + 30
        continuation_id = None
        while time.time() < deadline:
            checkpoint = client.get(
                f"/task-checkpoints/{checkpoint_id}"
            ).json()
            continuation_id = checkpoint.get("continuationRunId")
            if continuation_id:
                break
            time.sleep(0.1)
        continuation = poll_conversation_run(
            client, continuation_id, timeout=30
        )
        assert continuation["status"] == "succeeded", continuation
        assert continuation["replanBudget"] == {
            "maxRevisions": 1,
            "usedRevisions": 1,
            "remainingRevisions": 0,
        }
        artifacts = client.get(
            f"/conversation-runs/{continuation_id}/artifacts"
        ).json()
        assert any(
            item["kind"] == "agent_plan_snapshot"
            and item["metadata"].get("revision") == 1
            for item in artifacts
        )


def test_three_failed_continuation_generations_terminate(
    isolated_stack: DemoStack,
) -> None:
    stack = isolated_stack
    restart_worker(stack, crash=True, disable_task_recovery=True)
    with stack.client() as client:
        conversation = client.post(
            "/conversations", json={"title": "接力预算"}
        ).json()
        created = client.post(
            f"/conversations/{conversation['id']}/agent-runs",
            json={
                "content": (
                    "任务 [[MODE:planned_lookup_transform]]"
                    "[[RECOVERY_ALWAYS_FAIL]]"
                ),
                "allowedCapabilities": ["lookup.table"],
            },
        )
        source = poll_conversation_run(
            client, created.json()["conversationRunId"]
        )
        checkpoint_id = source["error"]["taskCheckpointId"]
        restart_worker(stack, crash=True)
        deadline = time.time() + 45
        exhausted = None
        while time.time() < deadline:
            checkpoints = []
            current_id = checkpoint_id
            for _ in range(4):
                response = client.get(
                    f"/task-checkpoints/{current_id}"
                )
                if response.status_code != 200:
                    break
                item = response.json()
                checkpoints.append(item)
                continuation_id = item.get("continuationRunId")
                if not continuation_id:
                    break
                continuation = client.get(
                    f"/conversation-runs/{continuation_id}"
                ).json()
                child_id = (continuation.get("error") or {}).get(
                    "taskCheckpointId"
                )
                if not child_id:
                    break
                current_id = child_id
            exhausted = next(
                (
                    item
                    for item in checkpoints
                    if item["status"] == "terminal"
                    and item.get("budgetExhausted")
                ),
                None,
            )
            if exhausted is not None:
                break
            time.sleep(0.2)
        assert exhausted is not None
        assert exhausted["continuationGeneration"] == 3
        assert exhausted["budgetExhausted"]["actualGenerations"] == 3


def test_e2e_10_progressive_catalog_composition_and_version_pinning(
    stack: DemoStack,
) -> None:
    with stack.client() as client:
        session_id = _workflow_session(client)
        searched = client.post(
            "/workflow-tools/search",
            json={
                "sessionId": session_id,
                "query": "问候",
                "constraints": {"sideEffects": "none"},
                "limit": 8,
            },
        )
        assert searched.status_code == 200, searched.text
        cards = searched.json()["items"]
        assert 1 <= len(cards) <= 8
        card = next(item for item in cards if item["name"] == "问候工作流")
        assert "inputSchema" not in card
        assert "definition" not in card

        described = client.post(
            "/workflow-tools/describe",
            json={"sessionId": session_id, "ref": card["workflowRef"]},
        )
        assert described.status_code == 200, described.text
        detail = described.json()["workflow"]
        assert detail["versionLock"]["digest"] in card["workflowRef"]
        assert "inputSchema" in detail
        assert "definition" not in detail

        plan = client.post(
            "/plans",
            json={
                "sessionId": session_id,
                "plan": {
                    "inputSchema": {
                        "type": "object",
                        "required": ["name"],
                        "properties": {"name": {"type": "string"}},
                    },
                    "outputSchema": {
                        "type": "object",
                        "required": ["message"],
                        "properties": {"message": {"type": "string"}},
                    },
                    "steps": [
                        {
                            "id": "first",
                            "kind": "workflow",
                            "ref": card["workflowRef"],
                            "input": {"name": "${input.name}"},
                        },
                        {
                            "id": "second",
                            "kind": "workflow",
                            "ref": card["workflowRef"],
                            "input": {"name": "${first.message}"},
                        },
                        {
                            "id": "summary",
                            "kind": "agent",
                            "input": {"text": "${second.message}"},
                            "config": {
                                "prompt": "[[MODE:echo_input]] 输出 reply",
                                "outputSchema": {
                                    "type": "object",
                                    "required": ["reply"],
                                    "properties": {"reply": {"type": "string"}},
                                    "additionalProperties": False,
                                },
                                "allowedCapabilities": [],
                            },
                        },
                    ],
                    "edges": [
                        {"source": "first", "target": "second"},
                        {"source": "second", "target": "summary"},
                    ],
                    "output": {"message": "${summary.reply}"},
                }
            },
        )
        assert plan.status_code == 201, plan.text
        snapshot = plan.json()
        assert snapshot["preview"]["hasSideEffects"] is False
        assert snapshot["preview"]["callsAgent"] is True
        assert len(snapshot["sourceRefs"]) == 2

        # 更新源组件默认版本；已创建 PlanSnapshot 仍引用旧 digest。
        component = _component(client, "确定性问候")
        workflow = client.get(f"/workflows/{component['workflowId']}").json()
        definition = workflow["definition"]
        format_node = next(
            node for node in definition["spec"]["nodes"] if node["id"] == "format"
        )
        format_node["config"]["template"] = "新版你好，{name}"
        updated = client.put(
            f"/workflows/{component['workflowId']}/draft",
            json={"revision": workflow["draftRevision"], "definition": definition},
        )
        assert updated.status_code == 200, updated.text
        published = _publish_workflow(client, component["workflowId"])
        assert published.status_code == 201, published.text
        changed_default = client.post(
            "/components",
            json={
                "name": "确定性问候",
                "description": component["description"],
                "workflowVersionId": published.json()["id"],
            },
        )
        assert changed_default.status_code == 201, changed_default.text

        plan_run = client.post(
            f"/plans/{snapshot['id']}/runs", json={"input": {"name": "Alice"}}
        )
        assert plan_run.status_code == 202, plan_run.text
        completed = poll_run(client, plan_run.json()["id"])
        assert completed["status"] == "succeeded", completed
        assert completed["output"] == {"message": "你好，你好，Alice"}
        assert completed["workflowVersionId"] is None
        assert completed["planSnapshotId"] == snapshot["id"]


def test_e2e_11_fragment_disclosure_and_slice_safety(stack: DemoStack) -> None:
    with stack.client() as client:
        session_id = _workflow_session(client)
        source = _component(client, "确定性问候")
        source_definition = client.get(f"/components/{source['id']}").json()[
            "definition"
        ]
        source_definition["metadata"] = {
            "name": "fragment-demo",
            "capability": {
                "summary": "可组合的问候片段",
                "useWhen": ["需要问候片段"],
                "tags": ["fragment"],
            },
            "callableFragments": [
                {
                    "name": "format-only",
                    "nodeIds": ["format"],
                    "summary": "只执行格式化",
                    "outputMapping": {"message": "${format.output.text}"},
                }
            ],
        }
        created = client.post(
            "/workflows",
            json={"name": "片段问候工作流", "definition": source_definition},
        )
        assert created.status_code == 201, created.text
        published = _publish_workflow(client, created.json()["id"])
        assert published.status_code == 201, published.text
        workflow_ref = next(
            item["workflowRef"]
            for item in client.post(
                "/workflow-tools/search",
                json={
                    "sessionId": session_id,
                    "query": "可组合的问候片段",
                    "limit": 8,
                },
            ).json()["items"]
            if item["workflowRef"] == published.json()["workflowRef"]
        )

        described = client.post(
            "/workflow-tools/describe",
            json={"sessionId": session_id, "ref": workflow_ref},
        ).json()["workflow"]
        fragment_ref = described["fragments"][0]["fragmentRef"]
        fragment = client.post(
            "/workflow-tools/describe-fragment",
            json={"sessionId": session_id, "ref": fragment_ref},
        )
        assert fragment.status_code == 200, fragment.text
        assert fragment.json()["fragment"]["nodeIds"] == ["format"]

        plan = client.post(
            "/plans",
            json={
                "sessionId": session_id,
                "plan": {
                    "inputSchema": source_definition["spec"]["inputSchema"],
                    "outputSchema": source_definition["spec"]["outputSchema"],
                    "steps": [
                        {
                            "id": "fragment",
                            "kind": "fragment",
                            "ref": fragment_ref,
                            "input": {"name": "${input.name}"},
                        }
                    ],
                    "output": {"message": "${fragment.message}"},
                }
            },
        )
        assert plan.status_code == 201, plan.text
        run = client.post(
            f"/plans/{plan.json()['id']}/runs", json={"input": {"name": "片段"}}
        )
        completed = poll_run(client, run.json()["id"])
        assert completed["status"] == "succeeded", completed
        source_template = next(
            node
            for node in source_definition["spec"]["nodes"]
            if node["id"] == "format"
        )["config"]["template"]
        assert completed["output"] == {
            "message": source_template.format(name="片段")
        }

        browser = _component(client, "费用提交（真实浏览器）")
        browser_definition = client.get(f"/components/{browser['id']}").json()[
            "definition"
        ]
        side_effect_node = next(
            node for node in browser_definition["spec"]["nodes"] if node.get("sideEffect")
        )
        browser_card = next(
            item
            for item in client.post(
                "/workflow-tools/search",
                json={
                    "sessionId": session_id,
                    "query": "费用提交",
                    "limit": 8,
                },
            ).json()["items"]
            if "费用提交" in item["name"]
        )
        rejected = client.post(
            "/workflow-tools/slice",
            json={
                "sessionId": session_id,
                "workflowRef": browser_card["workflowRef"],
                "candidateNodeIds": [side_effect_node["id"]],
            },
        )
        assert rejected.status_code == 422, rejected.text
        assert rejected.json()["detail"]["code"] == "FRAGMENT_SLICE_REJECTED"


def test_e2e_8b_real_browser_distillation_to_non_fake_agent(
    stack: DemoStack,
) -> None:
    with stack.client() as client:
        article_url = f"{stack.form_url}/article"
        stack.set_article("探索阶段正文")
        conversation = client.post(
            "/conversations", json={"title": "真实 Browser 固化"}
        ).json()
        started = client.post(
            f"/conversations/{conversation['id']}/agent-runs",
            json={
                "content": f"{article_url} [[MODE:browser_summary]]",
                "allowedCapabilities": ["browser.read", "summarize.text"],
            },
        )
        assert started.status_code == 202, started.text
        conversation_run_id = started.json()["conversationRunId"]
        summary = poll_conversation_run(client, conversation_run_id)
        assert summary["status"] == "succeeded", summary
        events = client.get(
            f"/conversation-runs/{conversation_run_id}/events"
        ).json()
        assert {"tool.requested", "tool.result"} <= {
            event["type"] for event in events
        }
        browser_completed = next(
            event for event in events if event["type"] == "tool.result"
        )
        assert browser_completed["payload"]["output"]["digest"]
        assert browser_completed["payload"]["evidenceDigest"].startswith("sha256:")
        assert summary["output"] == {"reply": "探索阶段正文"}
        evidence = summary["browserEvidence"][0]
        assert evidence["resultSource"] == "host_tool_broker"
        assert evidence["contentSha256"] == hashlib.sha256(
            "探索阶段正文".encode()
        ).hexdigest()

        distilled = client.post(f"/conversation-runs/{conversation_run_id}/distill")
        assert distilled.status_code == 201, distilled.text
        analysis = distilled.json()["analysis"]
        assert analysis["proposalSource"] == "distillation_agent"
        assert analysis["proposalDigest"].startswith("sha256:")
        assert analysis["sourceTrajectoryDigest"].startswith("sha256:")
        assert analysis["directNodes"] == 1
        assert analysis["agentNodes"] == 1
        assert analysis["publishable"] is True

        published = _publish_workflow(
            client, distilled.json()["draftWorkflowId"]
        )
        assert published.status_code == 201, published.text
        version_id = published.json()["id"]
        stack.set_article("第二次运行的新正文：利润增长 21%。")
        second_url = f"{article_url}?version=b"
        second_conversation = client.post(
            "/conversations", json={"title": "新对话语义复用"}
        ).json()
        selected = client.post(
            f"/conversations/{second_conversation['id']}/messages",
            json={"content": f"访问并总结这个网页：{second_url}"},
        )
        assert selected.status_code == 201, selected.text
        assistant = selected.json()["assistant"]
        assert assistant["state"] == "workflow_run"
        assert assistant["agentAction"] == "run_full"
        assert assistant["workflowVersionId"] == version_id
        assert {item["level"] for item in assistant["toolTimeline"]} == {"L0", "L1"}
        described = client.post(
            "/workflow-tools/describe",
            json={
                "sessionId": assistant["disclosureSessionId"],
                "ref": assistant["refs"][0],
            },
        )
        assert described.status_code == 200, described.text
        assert described.json()["workflow"]["lineage"]["sourceConversationRunId"] == (
            conversation_run_id
        )

        completed = poll_run(client, assistant["runId"], timeout=60)
        assert completed["status"] == "succeeded", completed
        assert "利润增长 21%" in completed["output"]["reply"]
        node_types = {
            node["nodeId"]: node["type"] for node in completed.get("nodes", [])
        }
        assert "browser" in node_types.values()
        assert "agent" in node_types.values()
        assert "agent.completed" in _events(client, completed["id"])


def test_e2e_12_successful_plan_only_suggests_human_draft(
    stack: DemoStack,
) -> None:
    with stack.client() as client:
        session_id = _workflow_session(client)
        card = client.post(
            "/workflow-tools/search",
            json={"sessionId": session_id, "query": "问候", "limit": 8},
        ).json()["items"][0]
        plan = client.post(
            "/plans",
            json={
                "sessionId": session_id,
                "plan": {
                    "inputSchema": {
                        "type": "object",
                        "required": ["name"],
                        "properties": {"name": {"type": "string"}},
                    },
                    "outputSchema": {
                        "type": "object",
                        "required": ["message"],
                        "properties": {"message": {"type": "string"}},
                    },
                    "steps": [
                        {
                            "id": "greet",
                            "kind": "workflow",
                            "ref": card["workflowRef"],
                            "input": {"name": "${input.name}"},
                        }
                    ],
                    "output": {"message": "${greet.message}"},
                }
            },
        )
        assert plan.status_code == 201, plan.text
        snapshot = plan.json()
        for name in ("A", "B", "C"):
            created = client.post(
                f"/plans/{snapshot['id']}/runs", json={"input": {"name": name}}
            )
            completed = poll_run(client, created.json()["id"])
            assert completed["status"] == "succeeded", completed

        suggestions = client.get("/plan-suggestions", params={"threshold": 3})
        assert suggestions.status_code == 200, suggestions.text
        suggestion = next(
            item
            for item in suggestions.json()
            if item["planSignature"] == snapshot["planSignature"]
        )
        assert suggestion["action"] == "suggest_draft"
        solidified = client.post(
            f"/plans/{snapshot['id']}/solidify", json={"name": "人工固化问候"}
        )
        assert solidified.status_code == 201, solidified.text
        assert solidified.json()["published"] is False
        draft = client.get(
            f"/workflows/{solidified.json()['workflowId']}"
        )
        assert draft.status_code == 200, draft.text
        assert (
            draft.json()["definition"]["metadata"]["sourcePlanSnapshotId"]
            == snapshot["id"]
        )


def test_e2e_7b_recovery_worker_crash_is_reclaimed(stack: DemoStack) -> None:
    with stack.client() as client:
        component = _component(client, "检查点接力演示")
        created = client.post(
            f"/components/{component['id']}/runs",
            json={"input": {"seed": "crash-recovery"}},
        )
        assert created.status_code == 202, created.text
        run_id = created.json()["id"]
        deadline = time.time() + 30
        claimed = False
        while time.time() < deadline:
            detail = client.get(f"/runs/{run_id}").json()
            claimed = any(
                checkpoint["status"] == "running"
                for checkpoint in detail.get("checkpoints", [])
            )
            if claimed:
                break
            time.sleep(0.1)
        assert claimed, "Recovery checkpoint 未被 Worker 领取"

        restart_worker(stack, crash=True)
        completed = poll_run(client, run_id, timeout=30)
        assert completed["status"] == "succeeded", completed
        detail = client.get(f"/runs/{run_id}").json()
        recovery_attempts = [
            node for node in detail["nodes"] if node["nodeId"] == "recovery_agent"
        ]
        assert [node["status"] for node in recovery_attempts] == [
            "lease_expired",
            "succeeded",
        ]
        risky_attempts = [
            node for node in detail["nodes"] if node["nodeId"] == "risky"
        ]
        assert all(node["status"] == "failed" for node in risky_attempts)
