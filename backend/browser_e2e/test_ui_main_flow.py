from __future__ import annotations

import os
import signal
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import expect, sync_playwright

from e2e.harness import start_stack, stop_stack

REPO_DIR = Path(__file__).resolve().parents[2]
FRONTEND_DIR = REPO_DIR / "frontend"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_http(url: str, process: subprocess.Popen, timeout: float = 30) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Vite 在就绪前退出，返回码 {process.returncode}")
        try:
            if httpx.get(url, timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise TimeoutError("等待 Vite 就绪超时")


def _submit(page, prompt: str) -> None:
    page.get_by_label("对话输入").fill(prompt)
    page.get_by_role("button", name="发送").click()


def test_real_ui_natural_exploration_reuse_and_run_detail() -> None:
    frontend_port = _free_port()
    frontend_url = f"http://127.0.0.1:{frontend_port}"
    stack = start_stack(
        model_env={"SKILLX_CORS_ORIGINS": f'["{frontend_url}"]'}
    )
    frontend_log_path = stack.workdir / "frontend.log"
    frontend_log = frontend_log_path.open("w", encoding="utf-8")
    stack._logs["frontend"] = frontend_log_path
    stack._log_handles.append(frontend_log)
    frontend = subprocess.Popen(
        [
            "npm",
            "run",
            "dev",
            "--",
            "--host",
            "127.0.0.1",
            "--port",
            str(frontend_port),
        ],
        cwd=FRONTEND_DIR,
        env={**os.environ, "VITE_API_BASE": stack.base_url},
        stdout=frontend_log,
        stderr=subprocess.STDOUT,
    )
    failed = False
    try:
        _wait_http(frontend_url, frontend)
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=True)
            except PlaywrightError as exc:
                reason = str(exc)
                if any(
                    marker in reason.lower()
                    for marker in ("permission", "sandbox", "executable doesn't exist")
                ):
                    pytest.skip(f"Chromium 无法在当前环境启动：{reason}")
                raise
            page = browser.new_page()
            console_errors: list[str] = []
            page.on(
                "console",
                lambda message: (
                    console_errors.append(message.text)
                    if message.type == "error"
                    else None
                ),
            )

            stack.set_article("016 UI 首次探索正文")
            page.goto(f"{frontend_url}/chat")
            page.wait_for_url("**/chat/*")
            _submit(
                page,
                (
                    f"请读取并总结 {stack.form_url}/article "
                    "[[SELECTION:continue_agent]][[MODE:browser_summary]]"
                ),
            )
            expect(page.get_by_text("016 UI 首次探索正文")).to_be_visible(
                timeout=60_000
            )
            page.get_by_role("button", name="固化为组件").click()
            page.get_by_role("link", name="打开固化草稿").click()
            expect(page.get_by_role("heading", name="网页内容读取与总结")).to_be_visible()
            page.get_by_role("button", name="静态检查").click()
            with page.expect_response(
                lambda response: response.url.endswith("/publish")
                and response.status == 201,
                timeout=30_000,
            ):
                page.get_by_role("button", name="发布版本").click()

            stack.set_article("016 UI 第二次复用正文")
            page.goto(f"{frontend_url}/chat")
            page.wait_for_url("**/chat/*")
            _submit(page, f"请访问并总结 {stack.form_url}/article?version=second")
            expect(page.get_by_text("016 UI 第二次复用正文")).to_be_visible(
                timeout=60_000
            )
            page.get_by_role("link", name="查看运行").click()
            expect(page.get_by_role("heading", name="运行详情")).to_be_visible()
            expect(page.get_by_role("heading", name="最终输出")).to_be_visible()
            expect(page.get_by_text("browser-read-summary.json")).to_be_visible()

            stack.set_transient_healthy(False)
            page.goto(f"{frontend_url}/chat")
            page.wait_for_url("**/chat/*")
            _submit(page, f"请访问并总结 {stack.form_url}/transient?mode=503")
            expect(page.get_by_text("INVALID_INPUT")).to_be_visible(
                timeout=60_000
            )
            page.get_by_role("link", name="查看运行").click()
            expect(page.get_by_role("heading", name="运行错误")).to_be_visible()
            expect(page.get_by_role("heading", name="检查点接力")).to_be_visible()

            page.goto(f"{frontend_url}/components")
            expect(page.get_by_role("heading", name="已发布组件")).to_be_visible()
            expect(page.get_by_text("网页内容读取与总结").first).to_be_visible()
            assert not console_errors, console_errors
            browser.close()
    except pytest.skip.Exception:
        raise
    except BaseException:
        failed = True
        raise
    finally:
        if frontend.poll() is None:
            frontend.send_signal(signal.SIGTERM)
            try:
                frontend.wait(timeout=10)
            except subprocess.TimeoutExpired:
                frontend.kill()
        stop_stack(stack, failed=failed)
