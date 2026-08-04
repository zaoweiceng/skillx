"""六场景黑盒验收 Harness。

为每次验收创建独立的临时目录、SQLite 数据库和 Artifact 根目录；只通过
`alembic upgrade head` 建库并 seed；启动独立的 API、Worker 与本地表单站点子进程；
通过 HTTP/SSE 驱动场景。端口动态分配；结束后优雅停止子进程；失败时保留临时目录并打印
所有子进程日志，成功时清理临时目录。
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import mkdtemp
from typing import Any

import httpx

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent
TRAEX_STUB = REPO_DIR / "examples" / "traex_stub.py"
FORM_SITE = REPO_DIR / "examples" / "local_form_site.py"


@dataclass
class DemoStack:
    base_url: str
    form_url: str
    workdir: Path
    _api: subprocess.Popen
    _worker: subprocess.Popen
    _form: subprocess.Popen
    _logs: dict[str, Path] = field(default_factory=dict)
    # 父进程持有的日志文件句柄，停止时显式关闭，避免泄漏描述符。
    _log_handles: list[Any] = field(default_factory=list)
    _env: dict[str, str] = field(default_factory=dict)

    def client(self) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, timeout=30)

    def form_stats(self) -> dict:
        response = httpx.get(f"{self.form_url}/__stats", timeout=5)
        response.raise_for_status()
        return response.json()

    def set_article(self, body: str) -> None:
        """修改动态文章页正文（模拟发布后页面内容改变）。"""

        response = httpx.get(f"{self.form_url}/__set-article", params={"body": body}, timeout=5)
        response.raise_for_status()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_http(url: str, timeout: float, proc: subprocess.Popen) -> None:
    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"进程在就绪前退出，返回码 {proc.returncode}")
        try:
            response = httpx.get(url, timeout=2)
            if response.status_code == 200:
                return
            last_error = f"{response.status_code}: {response.text[:200]}"
        except httpx.HTTPError as exc:
            last_error = str(exc)
        time.sleep(0.25)
    raise TimeoutError(f"等待 {url} 就绪超时：{last_error}")


def _wait_worker_polling(
    worker: subprocess.Popen,
    log_path: Path,
    timeout: float,
    marker: str = "worker polling",
) -> None:
    """确认 Worker 已进入领取循环，而不只是进程尚未退出。"""

    deadline = time.time() + timeout
    while time.time() < deadline:
        if worker.poll() is not None:
            raise RuntimeError(f"Worker 在就绪前退出，返回码 {worker.returncode}")
        if log_path.exists() and marker in log_path.read_text(encoding="utf-8"):
            return
        time.sleep(0.2)
    raise TimeoutError("Worker 未在超时内进入轮询状态")


def start_stack(
    *,
    port: int | None = None,
    form_port: int | None = None,
    model_env: dict[str, str] | None = None,
) -> DemoStack:
    port = port or _free_port()
    form_port = form_port or _free_port()
    workdir = Path(mkdtemp(prefix="skillx-demo-"))
    db_path = workdir / "demo.db"
    artifact_root = workdir / "artifacts"
    artifact_root.mkdir(parents=True, exist_ok=True)
    database_url = f"sqlite+aiosqlite:///{db_path}"

    env = {
        **os.environ,
        "SKILLX_DATABASE_URL": database_url,
        "SKILLX_ARTIFACT_ROOT": str(artifact_root),
        "SKILLX_AUTO_CREATE_SCHEMA": "false",
        # 配置仓库内 TraeX-compatible fixture，使真实 Agent 主路径可达。
        "SKILLX_TRAEX_COMMAND": f'["{sys.executable}", "{TRAEX_STUB}"]',
        "SKILLX_TRAEX_ALLOWED_EXECUTABLES": f'["{Path(sys.executable).name}"]',
        # Host policy 授予的 Agent Tool 集合：请求方能力只能取交集缩小，不能扩大。
        "SKILLX_AGENT_HOST_CAPABILITIES": (
            '["search.web", "lookup.table", "browser.read", "summarize.text"]'
        ),
        "SKILLX_AGENT_LEASE_SECONDS": "2",
        # 本地表单站点 URL：seed 据此创建真实 Playwright 提交组件（E2E-6）。
        "SKILLX_DEMO_FORM_URL": f"http://127.0.0.1:{form_port}/apply",
        # 动态文章页 URL：seed 据此创建动态网页总结组件（F-01）。
        "SKILLX_DEMO_ARTICLE_URL": f"http://127.0.0.1:{form_port}/article",
        **(model_env or {}),
    }

    # 只通过 Alembic 建库，禁止 Base.metadata.create_all()。
    migrate = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if migrate.returncode != 0:
        raise RuntimeError(f"Alembic 迁移失败：{migrate.stderr}")

    seed = subprocess.run(
        [sys.executable, "-m", "app.seed"],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if seed.returncode != 0:
        raise RuntimeError(f"Seed 失败：{seed.stderr}")

    api_log = open(workdir / "api.log", "w", encoding="utf-8")  # noqa: SIM115
    worker_log_path = workdir / "worker.log"
    worker_log = open(worker_log_path, "w", encoding="utf-8")  # noqa: SIM115
    form_log = open(workdir / "form.log", "w", encoding="utf-8")  # noqa: SIM115
    logs = {
        "api": workdir / "api.log",
        "worker": worker_log_path,
        "form": workdir / "form.log",
    }
    api = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=BACKEND_DIR,
        env=env,
        stdout=api_log,
        stderr=subprocess.STDOUT,
    )
    worker = subprocess.Popen(
        [sys.executable, "-m", "app.worker", "--worker-id", "demo-worker"],
        cwd=BACKEND_DIR,
        env=env,
        stdout=worker_log,
        stderr=subprocess.STDOUT,
    )
    form = subprocess.Popen(
        [sys.executable, "-m", "examples.local_form_site", "--port", str(form_port)],
        cwd=REPO_DIR,
        env=env,
        stdout=form_log,
        stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{port}/api"
    form_url = f"http://127.0.0.1:{form_port}"
    stack = DemoStack(
        base_url,
        form_url,
        workdir,
        api,
        worker,
        form,
        logs,
        [api_log, worker_log, form_log],
        env,
    )
    try:
        _wait_http(f"{base_url}/health/ready", timeout=30, proc=api)
        _wait_worker_polling(worker, worker_log_path, timeout=30)
        _wait_http(f"{form_url}/__stats", timeout=15, proc=form)
    except Exception:
        stop_stack(stack, failed=True)
        raise
    return stack


def restart_worker(stack: DemoStack, *, crash: bool = False) -> None:
    if stack._worker.poll() is None:
        if crash:
            stack._worker.kill()
        else:
            stack._worker.send_signal(signal.SIGTERM)
        stack._worker.wait(timeout=10)
    worker_log_path = stack._logs["worker"]
    worker_log = open(worker_log_path, "a", encoding="utf-8")  # noqa: SIM115
    stack._log_handles.append(worker_log)
    stack._worker = subprocess.Popen(
        [sys.executable, "-m", "app.worker", "--worker-id", "demo-worker-restarted"],
        cwd=BACKEND_DIR,
        env=stack._env,
        stdout=worker_log,
        stderr=subprocess.STDOUT,
    )
    _wait_worker_polling(
        stack._worker,
        worker_log_path,
        timeout=30,
        marker="worker polling worker_id=demo-worker-restarted",
    )


def stop_stack(stack: DemoStack, *, failed: bool = False) -> None:
    for proc in (stack._worker, stack._api, stack._form):
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
    for proc in (stack._worker, stack._api, stack._form):
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    # 显式关闭父进程持有的日志文件句柄，再读取内容/清理目录。
    for handle in stack._log_handles:
        try:
            handle.close()
        except Exception:  # noqa: BLE001 - 关闭失败不影响后续清理。
            pass
    if failed:
        for name, path in stack._logs.items():
            if path.exists():
                sys.stderr.write(f"\n===== {name} 日志 ({path}) =====\n")
                sys.stderr.write(path.read_text(encoding="utf-8")[-4000:])
        sys.stderr.write(f"\n失败：临时目录已保留于 {stack.workdir}\n")
    else:
        shutil.rmtree(stack.workdir, ignore_errors=True)


def poll_run(client: httpx.Client, run_id: str, *, timeout: float = 30) -> dict:
    """轮询 Run 直到进入终态或等待审批。"""

    deadline = time.time() + timeout
    while time.time() < deadline:
        run = client.get(f"/runs/{run_id}").json()
        if run["status"] in {"succeeded", "failed", "cancelled", "waiting_approval"}:
            return run
        time.sleep(0.3)
    raise TimeoutError(f"Run {run_id} 未在超时内进入终态")


def poll_conversation_run(
    client: httpx.Client, conversation_run_id: str, *, timeout: float = 30
) -> dict:
    """轮询 ConversationRun 长任务 handle 直到进入终态。"""

    deadline = time.time() + timeout
    while time.time() < deadline:
        run = client.get(f"/conversation-runs/{conversation_run_id}").json()
        if run["status"] in {"succeeded", "failed", "cancelled"}:
            return run
        time.sleep(0.3)
    raise TimeoutError(f"ConversationRun {conversation_run_id} 未在超时内进入终态")
