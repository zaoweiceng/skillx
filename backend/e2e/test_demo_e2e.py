"""六条 Demo E2E 黑盒验收。

每条场景都通过真实 HTTP API、独立 API/Worker 子进程和由 Alembic 建的临时 SQLite
完成。失败时 harness 会打印子进程日志。运行：
    uv run pytest e2e -q
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Iterator

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


def _workflow_session(client: httpx.Client) -> str:
    created = client.post("/workflow-tools/sessions", json={})
    assert created.status_code == 201, created.text
    return created.json()["id"]


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
        assert candidate["selectionSource"] == "traex"
        assert candidate["disclosureLevel"] == "L0"
        assert candidate["workflowRef"].startswith("workflow:")
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
    """F-03：一次瞬态失败不生成补丁；可复现失败经真实 replay 通过后才生成 draft。"""

    with stack.client() as client:
        # 用检查点接力组件的已发布版本作为补丁基线（其 risky 节点会确定性失败）。
        component = _component(client, "反馈分类")
        version_id = component["defaultVersionId"]
        # 尚无失败观测：补丁不合格。
        rejected = client.post(
            f"/workflow-versions/{version_id}/workflow-patch",
            json={
                "sourceRunId": "no-such-run",
                "patch": {
                    "nodePatches": [{"nodeId": "classify", "changes": {"timeoutSeconds": 60}}]
                },
            },
        )
        assert rejected.status_code == 422, rejected.text
        assert rejected.json()["detail"]["code"] == "PATCH_NOT_ELIGIBLE"

        definition = {
            "apiVersion": "workflow.platform/v1",
            "kind": "Workflow",
            "metadata": {"name": "replay-proof"},
            "spec": {
                "inputSchema": {
                    "type": "object",
                    "required": ["x"],
                    "properties": {"x": {"type": "string"}},
                },
                "outputSchema": {
                    "type": "object",
                    "required": ["y"],
                    "properties": {"y": {"type": "string"}},
                },
                "nodes": [
                    {"id": "input", "type": "input", "displayName": "输入"},
                    {
                        "id": "work",
                        "type": "transform",
                        "displayName": "确定失败",
                        "input": {"value": "${input.x}"},
                        "config": {"operation": "fail_dependency"},
                    },
                    {
                        "id": "output",
                        "type": "output",
                        "displayName": "输出",
                        "input": {"y": "${work.output.value}"},
                    },
                ],
                "edges": [
                    {"source": "input", "target": "work"},
                    {"source": "work", "target": "output"},
                ],
            },
        }
        workflow = client.post(
            "/workflows", json={"name": "Replay Proof", "definition": definition}
        )
        published = client.post(
            f"/workflows/{workflow.json()['id']}/publish"
        ).json()
        replay_component = client.post(
            "/components",
            json={
                "name": "Replay Proof",
                "workflowVersionId": published["id"],
            },
        ).json()
        source_run_id = ""
        for _ in range(2):
            created = client.post(
                f"/components/{replay_component['id']}/runs",
                json={"input": {"x": "safe"}},
            )
            failed = poll_run(client, created.json()["id"])
            assert failed["status"] == "failed", failed
            source_run_id = failed["id"]

        patched = client.post(
            f"/workflow-versions/{published['id']}/workflow-patch",
            json={
                "sourceRunId": source_run_id,
                "patch": {
                    "nodePatches": [
                        {
                            "nodeId": "work",
                            "changes": {"config": {"operation": "passthrough"}},
                        }
                    ]
                },
            },
        )
        assert patched.status_code == 201, patched.text
        proof = patched.json()
        assert proof["replay"]["baselineReproduced"] is True
        assert proof["replay"]["passed"] is True
        assert proof["replay"]["replayEvidenceId"]
        assert proof["draftWorkflowId"]


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
        published = client.post(f"/workflows/{component['workflowId']}/publish")
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
        published = client.post(f"/workflows/{created.json()['id']}/publish")
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
        assert "browser.action.started" in {event["type"] for event in events}
        browser_completed = next(
            event for event in events if event["type"] == "browser.action.completed"
        )
        assert browser_completed["payload"]["result"]["digest"]
        evidence = summary["browserEvidence"][0]
        assert evidence["resultSource"] == "browser_tool_broker"
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

        published = client.post(
            f"/workflows/{distilled.json()['draftWorkflowId']}/publish"
        )
        assert published.status_code == 201, published.text
        component = client.post(
            "/components",
            json={
                "name": "Browser 固化总结",
                "description": "真实 Browser 轨迹固化",
                "workflowVersionId": published.json()["id"],
            },
        )
        assert component.status_code == 201, component.text

        stack.set_article("第二次运行的新正文：利润增长 21%。")
        created = client.post(
            f"/components/{component.json()['id']}/runs",
            json={"input": {"message": article_url}},
        )
        completed = poll_run(client, created.json()["id"], timeout=60)
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
