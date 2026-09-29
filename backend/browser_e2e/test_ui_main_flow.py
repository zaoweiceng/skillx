from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import expect, sync_playwright

from e2e.harness import (
    restart_worker,
    start_stack,
    stop_stack,
)

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


@pytest.mark.parametrize("human", [False, True])
def test_durable_task_ui_and_manual_panels(human: bool) -> None:
    frontend_port = _free_port()
    frontend_url = f"http://127.0.0.1:{frontend_port}"
    stack = start_stack(model_env={"SKILLX_CORS_ORIGINS": f'["{frontend_url}"]'})
    log_path = stack.workdir / "frontend.log"
    log = log_path.open("w", encoding="utf-8")
    stack._logs["frontend"] = log_path
    stack._log_handles.append(log)
    frontend = subprocess.Popen(
        ["npm", "run", "dev", "--", "--host", "127.0.0.1", "--port", str(frontend_port)],
        cwd=FRONTEND_DIR,
        env={**os.environ, "VITE_API_BASE": stack.base_url},
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    failed = False
    try:
        _wait_http(frontend_url, frontend)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"{frontend_url}/chat")
            page.wait_for_url("**/chat/*")
            expect(page.get_by_role("button", name="工作流：开启")).to_have_count(0)
            with page.expect_response(
                lambda r: "/messages" in r.url and r.status == 202
            ) as response:
                _submit(page, "请问候用户 [[HUMAN]]" if human else "请问候 Alice")
            task_id = response.value.json()["taskId"]
            if human:
                expect(page.get_by_text("请提供姓名", exact=True)).to_be_visible(timeout=30_000)
                restart_worker(stack, crash=True)
                page.reload()
                expect(page.get_by_text("请提供姓名", exact=True)).to_be_visible(timeout=30_000)
                page.get_by_label("姓名", exact=False).fill("Bob")
                page.get_by_role("button", name="提交并继续").click()
            panel = page.get_by_role("region", name="任务执行")
            expect(panel.get_by_text("succeeded", exact=True).first).to_be_visible(timeout=30_000)
            expect(
                panel.get_by_text("你好，Bob" if human else "你好，Alice", exact=False).first
            ).to_be_visible()
            with httpx.Client(base_url=stack.base_url) as client:
                task = client.get(f"/tasks/{task_id}").json()
                assert task["status"] == "succeeded", task
                assert len(task["invocations"]) == 1
                assert not task["learning"]
                workflow_run = client.get(f"/runs/{task['invocations'][0]['runId']}").json()
                workflows = client.get("/workflows").json()
                workflow = next(x for x in workflows if x["name"] == "问候工作流")
                page.goto(f"{frontend_url}/workflows/{workflow['id']}/run")
                page.get_by_label("姓名", exact=False).fill("Carol")
                page.get_by_role("button", name="运行能力").click()
                page.get_by_role("link", name="运行详情").first.click()
                page.wait_for_url("**/runs/*")
                expect(
                    page.locator(".run-header").get_by_text("succeeded", exact=True)
                ).to_be_visible(timeout=30_000)
                assert workflow_run["status"] == "succeeded"
                registered = client.post(
                    "/capabilities",
                    json={"name": "text-stats", "type": "skillx", "packagePath": "text-stats"},
                )
                assert registered.status_code == 201, registered.text
                capabilities = client.get("/capabilities").json()
                assert capabilities
                page.goto(f"{frontend_url}/capabilities/{capabilities[0]['id']}")
                expect(page.get_by_role("heading", name="能力手动执行")).to_be_visible()
                page.get_by_label("文本", exact=False).fill("Hello world")
                page.get_by_role("button", name="运行能力").click()
                expect(page.get_by_text("succeeded", exact=True)).to_be_visible(timeout=30_000)
                if human:
                    component = next(
                        x
                        for x in client.get("/components").json()
                        if x["name"] == "费用提交（真实浏览器）"
                    )
                    created = client.post(
                        f"/components/{component['id']}/runs", json={"input": {"amount": 321}}
                    )
                    assert created.status_code == 202, created.text
                    run_id = created.json()["id"]
                    page.goto(f"{frontend_url}/runs/{run_id}")
                    expect(page.get_by_role("button", name="批准本次操作")).to_be_visible(
                        timeout=30_000
                    )
                    assert stack.form_stats()["submitCount"] == 0
                    restart_worker(stack, crash=True)
                    page.reload()
                    expect(page.get_by_role("button", name="批准本次操作")).to_be_visible(
                        timeout=30_000
                    )
                    with page.expect_response(
                        lambda r: "/human-interactions/" in r.url and r.url.endswith("/answer")
                    ) as approved:
                        page.get_by_role("button", name="批准本次操作").click()
                    assert approved.value.status == 200
                    expect(
                        page.locator(".run-header").get_by_text("succeeded", exact=True)
                    ).to_be_visible(timeout=30_000)
                    assert stack.form_stats()["submitCount"] == 1
            assert not errors, errors
            browser.close()
    except Exception:
        failed = True
        raise
    finally:
        frontend.terminate()
        frontend.wait(timeout=10)
        stop_stack(stack, failed=failed)
