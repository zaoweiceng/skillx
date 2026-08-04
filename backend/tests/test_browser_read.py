"""006-C-03：Browser Read/Extract adapter 只是一个 Adapter。

编译期用 BrowserReadContract 约束（URL/正文/字段 selector、输出 Schema）；运行期导航并
抽取正文与结构化字段作为节点输出。URL 来自输入 → 第二次运行以新输入重新取数，不使用
首次结果或常量正文。测试用 observedContent adapter 路径，不依赖真实浏览器。
"""

from __future__ import annotations

import asyncio

from app.domain.workflow import NodeType, WorkflowNode
from app.runtime.executors import BrowserContractExecutor, CancellationToken, RunContext


def _context() -> RunContext:
    return RunContext(
        run_id="run-1",
        run_input={},
        node_run_id="nr-1",
        cancellation=CancellationToken(asyncio.Event()),
    )


def _read_node(output_schema: dict | None = None) -> WorkflowNode:
    config = {
        "action": "read",
        "pageContract": {
            "contractVersion": "1",
            "urlPattern": "https://site.local/*",
            "bodySelector": "#main",
            "fields": {"title": "h1"},
        },
    }
    if output_schema is not None:
        config["pageContract"]["outputSchema"] = output_schema
    return WorkflowNode.model_validate(
        {
            "id": "read",
            "type": NodeType.BROWSER,
            "displayName": "读取网页",
            "input": {"url": "${input.url}"},
            "config": config,
        }
    )


def test_browser_read_extracts_body_and_fields() -> None:
    executor = BrowserContractExecutor()
    inputs = {
        "url": "https://site.local/a",
        "observedContent": {
            "url": "https://site.local/a",
            "title": "标题A",
            "content": "第一版正文",
            "fields": {"title": "标题A"},
        },
    }
    result = asyncio.run(executor.execute(_context(), _read_node(), inputs))
    assert result.status == "succeeded"
    assert result.output["content"] == "第一版正文"
    assert result.output["fields"] == {"title": "标题A"}


def test_browser_read_reflects_new_input_content() -> None:
    """第二次以新内容运行：输出随新正文变化，而非缓存首次结果。"""

    executor = BrowserContractExecutor()
    inputs = {
        "url": "https://site.local/a",
        "observedContent": {
            "url": "https://site.local/a",
            "content": "第二版正文——已修改",
            "fields": {},
        },
    }
    result = asyncio.run(executor.execute(_context(), _read_node(), inputs))
    assert result.status == "succeeded"
    assert result.output["content"] == "第二版正文——已修改"


def test_browser_read_rejects_url_outside_contract() -> None:
    executor = BrowserContractExecutor()
    inputs = {
        "url": "https://evil.example.com/a",
        "observedContent": {"content": "x", "fields": {}},
    }
    result = asyncio.run(executor.execute(_context(), _read_node(), inputs))
    assert result.status == "failed"
    assert result.error.code == "PAGE_DRIFT"


def test_browser_read_validates_output_schema() -> None:
    schema = {
        "type": "object",
        "properties": {"content": {"type": "string"}},
        "required": ["content"],
    }
    executor = BrowserContractExecutor()
    node = _read_node(schema)
    # 构造真正缺失场景：content 非字符串 → schema 校验失败 → fail closed。
    inputs2 = {"url": "https://site.local/a", "observedContent": {"content": 123, "fields": {}}}
    result2 = asyncio.run(executor.execute(_context(), node, inputs2))
    assert result2.status == "failed"
    assert result2.error.code == "CONTRACT_VIOLATION"
