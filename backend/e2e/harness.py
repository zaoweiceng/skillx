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

    def client(self) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, timeout=30)

    def form_stats(self) -> dict:
        response = httpx.get(f"{self.form_url}/__stats", timeout=5)
        response.raise_for_status()
        return response.json()


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


def _wait_worker_polling(worker: subprocess.Popen, log_path: Path, timeout: float) -> None:
    """确认 Worker 已进入领取循环，而不只是进程尚未退出。"""

    deadline = time.time() + timeout
    while time.time() < deadline:
        if worker.poll() is not None:
            raise RuntimeError(f"Worker 在就绪前退出，返回码 {worker.returncode}")
        if log_path.exists() and "worker polling" in log_path.read_text(encoding="utf-8"):
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
        # 本地表单站点 URL：seed 据此创建真实 Playwright 提交组件（E2E-6）。
        "SKILLX_DEMO_FORM_URL": f"http://127.0.0.1:{form_port}/apply",
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
    stack = DemoStack(base_url, form_url, workdir, api, worker, form, logs)
    try:
        _wait_http(f"{base_url}/health/ready", timeout=30, proc=api)
        _wait_worker_polling(worker, worker_log_path, timeout=30)
        _wait_http(f"{form_url}/__stats", timeout=15, proc=form)
    except Exception:
        stop_stack(stack, failed=True)
        raise
    return stack


def stop_stack(stack: DemoStack, *, failed: bool = False) -> None:
    for proc in (stack._worker, stack._api, stack._form):
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
    for proc in (stack._worker, stack._api, stack._form):
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
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
