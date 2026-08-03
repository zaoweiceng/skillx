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
    stack = start_stack(port=8801)
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
        assert candidate.get("candidateToken")
        # 补参并显式确认，启动锁定版本的 Workflow。
        confirmed = client.post(
            f"/conversations/{conversation['id']}/confirm-component",
            json={
                "componentId": candidate["componentId"],
                "candidateToken": candidate["candidateToken"],
                "inputs": {"name": "Bob"},
            },
        )
        assert confirmed.status_code == 201, confirmed.text
        run = poll_run(client, confirmed.json()["runId"])
        assert run["status"] == "succeeded"
        assert run["output"] == {"message": "你好，Bob"}


def test_e2e_5_agent_distillation_reuse(stack: DemoStack) -> None:
    with stack.client() as client:
        # 运行固化后的受限 Agent 组件，证明 Agent 类组件可复用。
        run = _run_component(client, "受限 Agent 摘要", {"topic": "季度复盘"})
        assert run["status"] == "succeeded"
        assert run["output"] == {"summary": "已完成受限 Agent 摘要"}
        assert "agent.completed" in _events(client, run["id"])


def test_e2e_6_browser_submit_with_one_time_approval(stack: DemoStack) -> None:
    observed = {
        "url": "https://forms.example.org/apply",
        "identity": "user-1",
        "businessObject": "expense",
        "fingerprint": "page-v1",
        "fields": [
            {
                "id": "amount",
                "required": True,
                "type": "number",
                "locator": {"role": "spinbutton", "label": "金额"},
            }
        ],
        "submit": {"role": "button", "name": "提交申请"},
    }
    with stack.client() as client:
        component = _component(client, "费用提交")
        created = client.post(
            f"/components/{component['id']}/runs",
            json={"input": {"observedPage": observed, "formValues": {"amount": 100}}},
        )
        assert created.status_code == 202, created.text
        run_id = created.json()["id"]
        run = poll_run(client, run_id)
        assert run["status"] == "waiting_approval"

        # 消费一次性提交授权。
        approvals = client.get(f"/runs/{run_id}").json()["approvals"]
        pending = next(item for item in approvals if item["status"] == "pending")
        approved = client.post(
            f"/approvals/{pending['id']}/approve",
            json={"snapshotDigest": pending["snapshotDigest"]},
        )
        assert approved.status_code == 200, approved.text
        run = poll_run(client, run_id)
        assert run["status"] == "succeeded"
        assert run["output"]["businessId"] == "EXP-DEMO-001"
        assert "browser.submit.completed" in _events(client, run_id)
