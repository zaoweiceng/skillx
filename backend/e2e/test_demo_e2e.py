"""六条 Demo E2E 黑盒验收。

每条场景都通过真实 HTTP API、独立 API/Worker 子进程和由 Alembic 建的临时 SQLite
完成。失败时 harness 会打印子进程日志。运行：
    uv run pytest e2e -q
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest

from e2e.harness import (
    DemoStack,
    poll_conversation_run,
    poll_run,
    start_stack,
    stop_stack,
)


@pytest.fixture(scope="module")
def stack(request: pytest.FixtureRequest) -> Iterator[DemoStack]:
    stack = start_stack()
    try:
        yield stack
    finally:
        # 若本模块内任一用例在 call 阶段失败，保留现场并打印子进程日志（B-03）。
        failed = bool(getattr(request.session, "_e2e_call_failed", False))
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


def test_e2e_4_conversation_component(stack: DemoStack) -> None:
    with stack.client() as client:
        conversation = client.post("/conversations", json={"title": "验收对话"}).json()
        message = client.post(
            f"/conversations/{conversation['id']}/messages",
            json={"content": "生成问候语 greeting 欢迎 welcome"},
        ).json()
        candidate = message["assistant"]
        assert candidate["type"] == "component_candidate"
        assert candidate.get("candidateSnapshotDigest")
        # 补参并显式确认，启动锁定版本的 Workflow。
        confirmed = client.post(
            f"/conversations/{conversation['id']}/confirm-component",
            json={
                "componentId": candidate["componentId"],
                "candidateSnapshotDigest": candidate["candidateSnapshotDigest"],
                "inputs": {"name": "Bob"},
            },
        )
        assert confirmed.status_code == 201, confirmed.text
        run = poll_run(client, confirmed.json()["runId"])
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

        # 4) 通过 API 人工发布并创建组件。
        published = client.post(f"/workflows/{draft_workflow_id}/publish")
        assert published.status_code == 201, published.text
        version_id = published.json()["id"]
        component = client.post(
            "/components",
            json={
                "name": "固化-季度复盘查表",
                "description": "由对话 Agent 探索固化并人工发布。",
                "workflowVersionId": version_id,
            },
        )
        assert component.status_code == 201, component.text
        component_id = component.json()["id"]

        # 5) 用与探索时不同的输入运行发布组件：结果随输入变化，且零模型调用。
        created = client.post(
            f"/components/{component_id}/runs",
            json={"input": {"message": "q2"}},
        )
        assert created.status_code == 202, created.text
        run = poll_run(client, created.json()["id"])
        assert run["status"] == "succeeded", run
        # 新输入 q2 选出不同结果（编译复用，而非重跑同一 prompt）。
        assert run["output"] == {"reply": "第二季度：营收增长 8%"}
        reuse_events = _events(client, run["id"])
        assert "agent.completed" not in reuse_events
        assert "llm.call.completed" not in reuse_events


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
        # sensitive（非幂等副作用）只执行一次，未被重放。
        nodes = client.get(f"/runs/{run_id}").json()["nodes"]
        sensitive_runs = [n for n in nodes if n["nodeId"] == "sensitive"]
        assert len(sensitive_runs) == 1
        assert sensitive_runs[0]["status"] == "succeeded"
