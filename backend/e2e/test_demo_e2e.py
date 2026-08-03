"""六条 Demo E2E 黑盒验收。

每条场景都通过真实 HTTP API、独立 API/Worker 子进程和由 Alembic 建的临时 SQLite
完成。失败时 harness 会打印子进程日志。运行：
    uv run pytest e2e -q
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest

from e2e.harness import DemoStack, poll_run, start_stack, stop_stack


@pytest.fixture(scope="module")
def stack() -> Iterator[DemoStack]:
    stack = start_stack()
    try:
        yield stack
    finally:
        stop_stack(stack)


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
        # 1) 通过 HTTP 创建对话并启动仓库内 TraeX-compatible Agent。
        conversation = client.post("/conversations", json={"title": "Agent 固化"}).json()
        started = client.post(
            f"/conversations/{conversation['id']}/agent-runs",
            json={"content": "探索并总结季度复盘 [[MODE:ok]][[REPLY:季度复盘要点]]"},
        )
        assert started.status_code == 201, started.text
        payload = started.json()
        assert payload["status"] == "succeeded"
        conversation_run_id = payload["conversationRunId"]

        # 2) 服务端生成成功 agent_run，事件含 agent.* 与 Tool 事件。
        summary = client.get(f"/conversation-runs/{conversation_run_id}").json()
        assert summary["runType"] == "agent_run"
        events = client.get(f"/conversation-runs/{conversation_run_id}/events").json()
        event_types = {item["type"] for item in events}
        assert {"agent.run.created", "agent.plan", "agent.run.succeeded"} <= event_types
        assert any(item["type"].startswith("tool.") for item in events)

        # 3) 固化只生成草稿，不产生 WorkflowVersion。
        distilled = client.post(f"/conversation-runs/{conversation_run_id}/distill")
        assert distilled.status_code == 201, distilled.text
        draft = distilled.json()
        draft_workflow_id = draft["draftWorkflowId"]
        assert draft["status"] == "draft_created"
        assert draft["analysis"]["sourceRunType"] == "agent_run"

        # 4) 通过 API 人工发布并创建组件。
        published = client.post(f"/workflows/{draft_workflow_id}/publish")
        assert published.status_code == 201, published.text
        version_id = published.json()["id"]
        component = client.post(
            "/components",
            json={
                "name": "固化-季度复盘 Agent",
                "description": "由对话 Agent 探索固化并人工发布。",
                "workflowVersionId": version_id,
            },
        )
        assert component.status_code == 201, component.text
        component_id = component.json()["id"]

        # 5) 用不同输入运行发布组件并验证复用输出。
        # 固化后的受限 Agent 复现该次探索：新输入作为数据注入，探索行为已冻结，
        # 因此以不同输入仍稳定产生结构化输出。
        created = client.post(
            f"/components/{component_id}/runs",
            json={"input": {"message": "换一个季度继续复用探索"}},
        )
        assert created.status_code == 202, created.text
        run = poll_run(client, created.json()["id"])
        assert run["status"] == "succeeded", run
        assert run["output"] == {"reply": "季度复盘要点"}
        assert "agent.completed" in _events(client, run["id"])


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
