"""六场景黑盒验收 Harness。

为每次验收创建独立的临时目录、SQLite 数据库和 Artifact 根目录；只通过
`alembic upgrade head` 建库并 seed；启动独立的 API 与 Worker 子进程；通过 HTTP/SSE
驱动场景。结束后优雅停止子进程，失败时保留日志。
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import mkdtemp

import httpx

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent


@dataclass
class DemoStack:
    base_url: str
    workdir: Path
    _api: subprocess.Popen
    _worker: subprocess.Popen
    _logs: dict[str, Path] = field(default_factory=dict)

    def client(self) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, timeout=30)


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


def start_stack(
    *,
    port: int = 8799,
    model_env: dict[str, str] | None = None,
) -> DemoStack:
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
    worker_log = open(workdir / "worker.log", "w", encoding="utf-8")  # noqa: SIM115
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
    base_url = f"http://127.0.0.1:{port}/api"
    try:
        _wait_http(f"{base_url}/health/ready", timeout=30, proc=api)
        if worker.poll() is not None:
            raise RuntimeError(f"Worker 在就绪前退出，返回码 {worker.returncode}")
    except Exception:
        stop_stack(
            DemoStack(base_url, workdir, api, worker, {"api": workdir / "api.log"}),
            failed=True,
        )
        raise
    return DemoStack(
        base_url,
        workdir,
        api,
        worker,
        {"api": workdir / "api.log", "worker": workdir / "worker.log"},
    )


def stop_stack(stack: DemoStack, *, failed: bool = False) -> None:
    for proc in (stack._worker, stack._api):
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
    for proc in (stack._worker, stack._api):
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    if failed:
        for name, path in stack._logs.items():
            if path.exists():
                sys.stderr.write(f"\n===== {name} 日志 ({path}) =====\n")
                sys.stderr.write(path.read_text(encoding="utf-8")[-4000:])


def poll_run(client: httpx.Client, run_id: str, *, timeout: float = 20) -> dict:
    """轮询 Run 直到进入终态或等待审批。"""

    deadline = time.time() + timeout
    while time.time() < deadline:
        run = client.get(f"/runs/{run_id}").json()
        if run["status"] in {"succeeded", "failed", "cancelled", "waiting_approval"}:
            return run
        time.sleep(0.3)
    raise TimeoutError(f"Run {run_id} 未在超时内进入终态")
