"""Harness 自测：验证失败留现场/成功清理策略被实际执行（不启动真实 stack）。

避免 REPLAY 中“靠注释声明日志策略”的问题：构造假的 DemoStack，直接断言
``stop_stack`` 的日志与目录行为。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from tempfile import mkdtemp

from e2e.harness import DemoStack, stop_stack


class _DoneProc:
    """模拟已退出的子进程，避免真实启动。"""

    returncode = 0

    def poll(self) -> int:
        return 0

    def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002
        return 0

    def send_signal(self, _sig: int) -> None:
        return None


def _stack(workdir: Path) -> DemoStack:
    return DemoStack(
        base_url="http://127.0.0.1:0/api",
        form_url="http://127.0.0.1:0",
        workdir=workdir,
        _api=_DoneProc(),  # type: ignore[arg-type]
        _worker=_DoneProc(),  # type: ignore[arg-type]
        _form=_DoneProc(),  # type: ignore[arg-type]
        _logs={"api": workdir / "api.log"},
    )


def test_failed_run_retains_workdir_and_prints_logs(capsys) -> None:
    workdir = Path(mkdtemp(prefix="skillx-harness-selftest-"))
    (workdir / "api.log").write_text("boom: 受控失败日志", encoding="utf-8")
    try:
        stop_stack(_stack(workdir), failed=True)
        # 失败时保留现场目录，并把日志片段打印到 stderr。
        assert workdir.exists()
        assert "受控失败日志" in capsys.readouterr().err
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_successful_run_cleans_workdir() -> None:
    workdir = Path(mkdtemp(prefix="skillx-harness-selftest-ok-"))
    (workdir / "api.log").write_text("ok", encoding="utf-8")
    stop_stack(_stack(workdir), failed=False)
    assert not workdir.exists()
