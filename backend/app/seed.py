from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from app.application.compiler import WorkflowCompiler
from app.application.mcp import classify_snapshot, discover_tools, register_server
from app.application.workflow_catalog import build_manifest
from app.infrastructure.database import (
    Component,
    SessionLocal,
    Workflow,
    WorkflowVersion,
)


async def seed() -> str:
    example = Path(__file__).resolve().parents[2] / "examples" / "greeting-workflow.json"
    definition = json.loads(example.read_text(encoding="utf-8"))
    compiled = WorkflowCompiler().compile(definition)
    async with SessionLocal() as session:
        component = await session.scalar(select(Component).where(Component.name == "确定性问候"))
        if component is None:
            component = await _create_component(
                session,
                name="确定性问候",
                description="输入姓名，Direct 节点生成结构化问候 greeting 欢迎语，不加载模型。",
                workflow_name="问候工作流",
                definition=definition,
                validation={
                    "topologicalOrder": compiled.topological_order,
                    "diagnostics": [],
                    "executionClass": "zero_llm",
                },
            )
        weather_component = await session.scalar(
            select(Component).where(Component.name == "本地天气查询")
        )
        if weather_component is None:
            server_file = Path(__file__).resolve().parents[2] / "examples" / "local_weather_mcp.py"
            _, server_version = await register_server(
                session,
                "local-weather",
                "stdio",
                {
                    "command": [sys.executable, str(server_file)],
                    "timeoutSeconds": 5,
                },
            )
            snapshot = (await discover_tools(session, server_version.id))[0]
            # 天气 Tool 仅有 readOnlyHint 提示，仍需本地 Registry 显式审核为
            # none/idempotent 才能锁定发布；分类产生新的不可变快照。
            snapshot = await classify_snapshot(
                session, snapshot.id, effect="none", idempotent=True
            )
            weather_definition = _weather_workflow(snapshot.id)
            weather_compiled = WorkflowCompiler().compile(weather_definition)
            await _create_component(
                session,
                name="本地天气查询",
                description="固定 MCP Tool 查询本地确定性天气数据，模型调用为零。",
                workflow_name="天气查询工作流",
                definition=weather_definition,
                validation={
                    "topologicalOrder": weather_compiled.topological_order,
                    "diagnostics": [],
                    "executionClass": "zero_llm",
                    "mcpLocks": [
                        {
                            "nodeId": "weather",
                            "mcpToolSnapshotId": snapshot.id,
                            "digest": snapshot.digest,
                            "toolName": snapshot.name,
                            "effect": snapshot.side_effect,
                            "idempotent": snapshot.idempotent,
                            "timeoutSeconds": snapshot.timeout_seconds,
                        }
                    ],
                },
            )
        if await session.scalar(select(Component).where(Component.name == "反馈分类")) is None:
            hybrid = _hybrid_workflow()
            await _create_component(
                session,
                name="反馈分类",
                description="固定 DAG 中单个 LLM 节点做分类，仅显式节点产生 Token。",
                workflow_name="反馈分类工作流",
                definition=hybrid,
                validation={"diagnostics": [], "executionClass": "hybrid"},
            )
        agent_exists = await session.scalar(
            select(Component).where(Component.name == "受限 Agent 摘要")
        )
        if agent_exists is None:
            agent = _agent_workflow()
            await _create_component(
                session,
                name="受限 Agent 摘要",
                description="固化后的受限 Agent 节点，用于演示 Agent 类组件运行。",
                workflow_name="Agent 摘要工作流",
                definition=agent,
                validation={"diagnostics": [], "executionClass": "agent"},
            )
        if await session.scalar(select(Component).where(Component.name == "费用提交")) is None:
            browser = _browser_workflow()
            await _create_component(
                session,
                name="费用提交",
                description="浏览器契约填写并提交，需一次性授权；页面/身份变化即失效。",
                workflow_name="费用提交工作流",
                definition=browser,
                validation={"diagnostics": [], "executionClass": "zero_llm"},
            )
        # 检查点接力演示组件：仅当配置 TraeX 时 seed，供黑盒 E2E-7 验证 fallback 恢复。
        if (
            os.environ.get("SKILLX_TRAEX_COMMAND")
            and await session.scalar(
                select(Component).where(Component.name == "检查点接力演示")
            )
            is None
        ):
            recovery = _recovery_workflow()
            await _create_component(
                session,
                name="检查点接力演示",
                description="中间节点注入可重试失败，重试耗尽后按 fallback 从 checkpoint 接力。",
                workflow_name="检查点接力工作流",
                definition=recovery,
                validation={"diagnostics": [], "executionClass": "agent"},
            )
        # 真实 Playwright 提交组件：仅当提供本地表单站点 URL 时 seed，供黑盒 E2E-6 使用。
        form_url = os.environ.get("SKILLX_DEMO_FORM_URL")
        if (
            form_url
            and await session.scalar(
                select(Component).where(Component.name == "费用提交（真实浏览器）")
            )
            is None
        ):
            playwright_browser = _playwright_browser_workflow(form_url)
            await _create_component(
                session,
                name="费用提交（真实浏览器）",
                description="真实 Playwright 打开本地表单站点，填写并在一次性授权后提交。",
                workflow_name="费用提交（真实浏览器）工作流",
                definition=playwright_browser,
                validation={"diagnostics": [], "executionClass": "zero_llm"},
            )
        # 动态网页总结组件：Browser Read/Extract → TraeX 总结的混合图；仅当同时提供
        # 站点 URL 与 TraeX 时 seed，供 F-01 验证“页面改变后重新读取并交给 TraeX”。
        article_url = os.environ.get("SKILLX_DEMO_ARTICLE_URL")
        if (
            article_url
            and os.environ.get("SKILLX_TRAEX_COMMAND")
            and await session.scalar(
                select(Component).where(Component.name == "动态网页总结")
            )
            is None
        ):
            web_summary = _web_summary_workflow(article_url)
            await _create_component(
                session,
                name="动态网页总结",
                description="确定读取网页正文（Browser Read）后交给受限 Agent 总结。",
                workflow_name="动态网页总结工作流",
                definition=web_summary,
                validation={"diagnostics": [], "executionClass": "agent"},
            )
        await session.commit()
        return component.id


async def _create_component(
    session,
    *,
    name: str,
    description: str,
    workflow_name: str,
    definition: dict,
    validation: dict,
) -> Component:
    compiled = WorkflowCompiler().compile(definition)
    workflow = Workflow(
        id=str(uuid4()),
        name=workflow_name,
        owner_id="local-user",
        draft_revision=1,
        draft_json=definition,
    )
    session.add(workflow)
    await session.flush()
    version = WorkflowVersion(
        id=str(uuid4()),
        workflow_id=workflow.id,
        version=1,
        definition_json=definition,
        digest=compiled.digest,
        validation_json=validation,
    )
    session.add(version)
    await session.flush()
    await build_manifest(session, workflow, version)
    component = Component(
        id=str(uuid4()),
        name=name,
        description=description,
        workflow_id=workflow.id,
        default_version_id=version.id,
        visibility="public",
        execution_class=validation.get("executionClass", "zero_llm"),
    )
    session.add(component)
    return component


def _weather_workflow(snapshot_id: str) -> dict:
    schema = {
        "type": "object",
        "properties": {
            "city": {"type": "string"},
            "condition": {"type": "string"},
            "temperature": {"type": "number"},
        },
        "required": ["city", "condition", "temperature"],
        "additionalProperties": False,
    }
    return {
        "apiVersion": "workflow.platform/v1",
        "kind": "Workflow",
        "metadata": {"name": "local-weather"},
        "spec": {
            "inputSchema": {
                "type": "object",
                "properties": {"city": {"type": "string", "title": "城市"}},
                "required": ["city"],
                "additionalProperties": False,
            },
            "outputSchema": schema,
            "nodes": [
                {"id": "input", "type": "input", "displayName": "输入"},
                {
                    "id": "weather",
                    "type": "mcp",
                    "displayName": "查询天气",
                    "input": {"city": "${input.city}"},
                    "config": {"mcpToolSnapshotId": snapshot_id},
                },
                {
                    "id": "output",
                    "type": "output",
                    "displayName": "输出",
                    "input": {
                        "city": "${weather.output.city}",
                        "condition": "${weather.output.condition}",
                        "temperature": "${weather.output.temperature}",
                    },
                },
            ],
            "edges": [
                {"source": "input", "target": "weather"},
                {"source": "weather", "target": "output"},
            ],
        },
    }


def _hybrid_workflow() -> dict:
    output_schema = {
        "type": "object",
        "properties": {"category": {"type": "string"}},
        "required": ["category"],
        "additionalProperties": False,
    }
    return {
        "apiVersion": "workflow.platform/v1",
        "kind": "Workflow",
        "metadata": {"name": "feedback-classify"},
        "spec": {
            "inputSchema": {
                "type": "object",
                "properties": {"text": {"type": "string", "title": "反馈内容"}},
                "required": ["text"],
                "additionalProperties": False,
            },
            "outputSchema": output_schema,
            "nodes": [
                {"id": "input", "type": "input", "displayName": "输入"},
                {
                    "id": "classify",
                    "type": "llm",
                    "displayName": "分类",
                    "input": {"text": "${input.text}"},
                    "config": {
                        "modelProfile": "fake",
                        "outputSchema": output_schema,
                        "fakeOutput": {"category": "feedback"},
                        "prompt": "对以下反馈分类：{input}",
                    },
                },
                {
                    "id": "output",
                    "type": "output",
                    "displayName": "输出",
                    "input": {"category": "${classify.output.category}"},
                },
            ],
            "edges": [
                {"source": "input", "target": "classify"},
                {"source": "classify", "target": "output"},
            ],
        },
    }


def _agent_workflow() -> dict:
    output_schema = {
        "type": "object",
        "properties": {"summary": {"type": "string"}},
        "required": ["summary"],
        "additionalProperties": False,
    }
    return {
        "apiVersion": "workflow.platform/v1",
        "kind": "Workflow",
        "metadata": {"name": "agent-summary"},
        "spec": {
            "inputSchema": {
                "type": "object",
                "properties": {"topic": {"type": "string", "title": "主题"}},
                "required": ["topic"],
                "additionalProperties": False,
            },
            "outputSchema": output_schema,
            "nodes": [
                {"id": "input", "type": "input", "displayName": "输入"},
                {
                    "id": "agent",
                    "type": "agent",
                    "displayName": "受限 Agent",
                    "input": {"topic": "${input.topic}"},
                    "config": {
                        "agentProfile": "fake",
                        "outputSchema": output_schema,
                        "fakeOutput": {"summary": "已完成受限 Agent 摘要"},
                        "maxSteps": 3,
                        "maxTotalTokens": 500,
                        "allowedCapabilities": [],
                    },
                },
                {
                    "id": "output",
                    "type": "output",
                    "displayName": "输出",
                    "input": {"summary": "${agent.output.summary}"},
                },
            ],
            "edges": [
                {"source": "input", "target": "agent"},
                {"source": "agent", "target": "output"},
            ],
        },
    }


def _recovery_workflow() -> dict:
    """检查点接力演示：sensitive(副作用成功) → risky(注入可重试失败, fallback→agent)。"""

    output_schema = {
        "type": "object",
        "properties": {"result": {"type": "string"}},
        "required": ["result"],
        "additionalProperties": False,
    }
    return {
        "apiVersion": "workflow.platform/v1",
        "kind": "Workflow",
        "metadata": {"name": "checkpoint-recovery"},
        "spec": {
            "inputSchema": {
                "type": "object",
                "properties": {"seed": {"type": "string", "title": "输入"}},
                "required": ["seed"],
                "additionalProperties": False,
            },
            "outputSchema": output_schema,
            "nodes": [
                {"id": "input", "type": "input", "displayName": "输入"},
                {
                    "id": "sensitive",
                    "type": "transform",
                    "displayName": "副作用步骤",
                    "input": {"value": "${input.seed}"},
                    "config": {"operation": "passthrough"},
                    "sideEffect": True,
                    "idempotent": False,
                },
                {
                    "id": "risky",
                    "type": "transform",
                    "displayName": "易失败步骤",
                    "input": {"value": "${sensitive.output.value}"},
                    "config": {"operation": "fail_dependency"},
                    "retry": {"maxAttempts": 2, "backoffSeconds": 0},
                    "fallback": {
                        "on": ["DEPENDENCY_UNAVAILABLE"],
                        "target": "recovery_agent",
                    },
                },
                {
                    "id": "recovery_agent",
                    "type": "agent",
                    "displayName": "恢复 Agent",
                    "input": {"seed": "${input.seed}"},
                    "config": {
                        "outputSchema": {"type": "object"},
                        "maxSteps": 4,
                        "maxTotalTokens": 2000,
                        "allowedCapabilities": [],
                    },
                },
                {
                    "id": "output",
                    "type": "output",
                    "displayName": "输出",
                    "input": {"result": "${risky.output.value}"},
                },
            ],
            "edges": [
                {"source": "input", "target": "sensitive"},
                {"source": "sensitive", "target": "risky"},
                {"source": "risky", "target": "output"},
            ],
        },
    }


def _browser_workflow() -> dict:
    contract = {
        "contractVersion": "1",
        "urlPattern": "https://forms.example.org/apply*",
        "pageIdentity": {"identity": "user-1", "businessObject": "expense"},
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
    return {
        "apiVersion": "workflow.platform/v1",
        "kind": "Workflow",
        "metadata": {"name": "expense-submit"},
        "spec": {
            "inputSchema": {
                "type": "object",
                "properties": {
                    "observedPage": {"type": "object", "title": "观察页面"},
                    "formValues": {"type": "object", "title": "表单值"},
                },
                "required": ["observedPage", "formValues"],
            },
            "outputSchema": {
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                    "businessId": {"type": "string"},
                    "evidence": {"type": "object"},
                },
                "required": ["status", "businessId", "evidence"],
            },
            "nodes": [
                {"id": "input", "type": "input", "displayName": "输入"},
                {
                    "id": "submit",
                    "type": "browser",
                    "displayName": "提交表单",
                    "input": {
                        "observedPage": "${input.observedPage}",
                        "formValues": "${input.formValues}",
                    },
                    "config": {
                        "pageContract": contract,
                        "submit": True,
                        "fakeSubmitResult": {
                            "status": "succeeded",
                            "businessId": "EXP-DEMO-001",
                        },
                    },
                    "sideEffect": True,
                    "idempotent": False,
                },
                {
                    "id": "output",
                    "type": "output",
                    "displayName": "输出",
                    "input": {
                        "status": "${submit.output.status}",
                        "businessId": "${submit.output.businessId}",
                        "evidence": "${submit.output.evidence}",
                    },
                },
            ],
            "edges": [
                {"source": "input", "target": "submit"},
                {"source": "submit", "target": "output"},
            ],
        },
    }


def _playwright_browser_workflow(form_url: str) -> dict:
    """真实 Playwright 提交工作流：打开本地表单站点，填写并在授权后提交。"""

    contract = {
        "contractVersion": "1",
        "urlPattern": form_url,
        "pageIdentity": {"identity": "user-1"},
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
    return {
        "apiVersion": "workflow.platform/v1",
        "kind": "Workflow",
        "metadata": {"name": "expense-submit-playwright"},
        "spec": {
            "inputSchema": {
                "type": "object",
                "properties": {"amount": {"type": "number", "title": "金额"}},
                "required": ["amount"],
                "additionalProperties": False,
            },
            "outputSchema": {
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                    "businessId": {"type": "string"},
                    "evidence": {"type": "object"},
                },
                "required": ["status", "businessId", "evidence"],
            },
            "nodes": [
                {"id": "input", "type": "input", "displayName": "输入"},
                {
                    "id": "submit",
                    "type": "browser",
                    "displayName": "提交费用",
                    "input": {"formValues": {"amount": "${input.amount}"}},
                    "config": {
                        "runtime": "playwright",
                        "url": form_url,
                        "pageContract": contract,
                        "identitySelector": "[data-testid='identity']",
                        "submit": True,
                        "successAssertions": [
                            {"type": "text", "value": "提交成功", "timeoutMs": 3000}
                        ],
                        "resultSelectors": {"businessId": "#business-id"},
                    },
                    "sideEffect": True,
                    "idempotent": False,
                    "timeoutSeconds": 30,
                },
                {
                    "id": "output",
                    "type": "output",
                    "displayName": "输出",
                    "input": {
                        "status": "${submit.output.status}",
                        "businessId": "${submit.output.businessId}",
                        "evidence": "${submit.output.evidence}",
                    },
                },
            ],
            "edges": [
                {"source": "input", "target": "submit"},
                {"source": "submit", "target": "output"},
            ],
        },
    }


def _web_summary_workflow(article_url: str) -> dict:
    """动态网页总结：input(url) → read(Browser Read/Extract) → summarize(受限 Agent) → output。

    read 节点用 BrowserReadContract 抽取 #main 正文；summarize 的输入引用 read 节点的
    content 输出（真实数据依赖），因此页面正文改变后第二次运行会读取新正文并交给 TraeX。
    """

    read_output_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "title": {"type": "string"},
            "content": {"type": "string"},
            "fields": {"type": "object"},
        },
        "required": ["content"],
    }
    summary_schema = {
        "type": "object",
        "properties": {"reply": {"type": "string"}},
        "required": ["reply"],
        "additionalProperties": False,
    }
    from urllib.parse import urlparse

    parsed = urlparse(article_url)
    url_pattern = f"{parsed.scheme}://{parsed.netloc}/*"
    return {
        "apiVersion": "workflow.platform/v1",
        "kind": "Workflow",
        "metadata": {"name": "web-summary"},
        "spec": {
            "inputSchema": {
                "type": "object",
                "properties": {"url": {"type": "string", "title": "文章 URL"}},
                "required": ["url"],
                "additionalProperties": False,
            },
            "outputSchema": summary_schema,
            "nodes": [
                {"id": "input", "type": "input", "displayName": "输入"},
                {
                    "id": "read",
                    "type": "browser",
                    "displayName": "读取网页正文",
                    "input": {"url": "${input.url}"},
                    "config": {
                        "runtime": "playwright",
                        "action": "read",
                        "pageContract": {
                            "contractVersion": "1",
                            "urlPattern": url_pattern,
                            "bodySelector": "#main",
                            "fields": {"headline": "#headline"},
                            "outputSchema": read_output_schema,
                        },
                    },
                    "timeoutSeconds": 30,
                },
                {
                    "id": "summarize",
                    "type": "agent",
                    "displayName": "受限 Agent 总结",
                    # 真实数据依赖：总结输入引用 read 节点抽取的正文，而非常量。
                    "input": {"message": "${read.output.content}"},
                    "config": {
                        "agentProfile": "fake",
                        "outputSchema": summary_schema,
                        "maxSteps": 2,
                        "maxTotalTokens": 500,
                        "allowedCapabilities": [],
                        # fake Agent 直接回显正文摘要，使输出依赖新正文（页面变化即变化）。
                        "echoInputField": "message",
                    },
                    "timeoutSeconds": 30,
                },
                {
                    "id": "output",
                    "type": "output",
                    "displayName": "输出",
                    "input": {"reply": "${summarize.output.reply}"},
                },
            ],
            "edges": [
                {"source": "input", "target": "read"},
                {"source": "read", "target": "summarize"},
                {"source": "summarize", "target": "output"},
            ],
        },
    }


def main() -> None:
    component_id = asyncio.run(seed())
    print(f"seeded component: {component_id}")


if __name__ == "__main__":
    main()
