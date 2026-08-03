"""E2E pytest 配置：让 harness 感知测试正文（call 阶段）的断言结果。

默认 fixture 的 finally 无条件清理临时目录，因此测试正文断言失败时不会打印日志、
也不保留现场（REPLAY 的 B-03 声明并未成立）。这里通过 ``pytest_runtest_makereport``
把每个用例各阶段的结果挂到 ``item``，fixture 据此在失败时调用
``stop_stack(failed=True)``，打印子进程日志并保留临时目录。
"""

from __future__ import annotations

from typing import Any

import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[Any]):
    outcome = yield
    report = outcome.get_result()
    # 把 setup/call/teardown 各阶段的 report 挂到 item，供 fixture 判断是否失败。
    setattr(item, f"rep_{report.when}", report)
    # call 阶段失败时在 session 上打标：模块级 stack fixture 据此保留现场并打印日志。
    if call.when == "call" and report.failed:
        item.session._e2e_call_failed = True  # type: ignore[attr-defined]
