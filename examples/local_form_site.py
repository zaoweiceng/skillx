#!/usr/bin/env python3
"""可重复的本地表单站点，供 E2E-6 真实 Playwright 提交验收使用。

提供一个费用申请表单页面与提交端点，记录提交次数与最近一次提交数据到内存计数器，
并暴露 ``/__stats`` 供测试断言“只提交一次”。仅监听 127.0.0.1，不对外暴露。

用法：
    python -m examples.local_form_site --port 8975 [--business-id EXP-DEMO-001]
"""

from __future__ import annotations

import argparse
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

_LOCK = threading.Lock()
_STATE: dict[str, object] = {
    "submitCount": 0,
    "lastSubmission": None,
    "mutated": False,
    "articleBody": "第一版正文：季度营收增长 12%。",
    "transientCount": 0,
    "alternateCount": 0,
    "transientHealthy": False,
}


def _form_page(business_id: str, mutated: bool) -> str:
    # data-contract-id 让契约字段可被 Playwright 稳定定位；提交后展示 businessId。
    # mutated 时新增一个必填字段，模拟审批后页面结构变化，使旧授权失效（L3 漂移）。
    extra = (
        '<label for="cost-center">成本中心</label>'
        '<input id="cost-center" name="cost-center" data-contract-id="cost-center" '
        'required aria-label="成本中心" />'
        if mutated
        else ""
    )
    return f"""<!doctype html>
<html lang="zh">
<head><meta charset="utf-8"><title>费用申请</title></head>
<body>
<span data-testid="identity">user-1</span>
<h1>费用申请</h1>
<form method="post" action="/submit">
  <label for="amount">金额</label>
  <input id="amount" name="amount" data-contract-id="amount" type="number" required
         aria-label="金额" />
  {extra}
  <button type="submit" role="button">提交申请</button>
</form>
<div id="result" data-business-id="{business_id}"></div>
</body>
</html>
"""


def _result_page(business_id: str, amount: str) -> str:
    return f"""<!doctype html>
<html lang="zh">
<head><meta charset="utf-8"><title>提交成功</title></head>
<body>
<h1>提交成功</h1>
<p id="status">申请已提交</p>
<p id="business-id">{business_id}</p>
<p id="amount">{amount}</p>
</body>
</html>
"""


def _article_page(body: str) -> str:
    # 供 Browser Read/Extract 读取的动态文章页；正文放在 #main，供 bodySelector 抽取。
    return f"""<!doctype html>
<html lang="zh">
<head><meta charset="utf-8"><title>季度文章</title></head>
<body>
<h1 id="headline">季度复盘</h1>
<article id="main">{body}</article>
</body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    business_id = "EXP-DEMO-001"

    def log_message(self, *_: object) -> None:  # noqa: D401 - 静默默认日志。
        return

    def _send_html(self, status: int, body: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, status: int, body: dict[str, object]) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in {"/", "/apply"}:
            with _LOCK:
                mutated = bool(_STATE["mutated"])
            self._send_html(200, _form_page(self.business_id, mutated))
        elif path == "/article":
            with _LOCK:
                body = str(_STATE["articleBody"])
            self._send_html(200, _article_page(body))
        elif path == "/transient":
            mode = parse_qs(urlparse(self.path).query).get(
                "mode", ["503"]
            )[0]
            with _LOCK:
                _STATE["transientCount"] = (
                    int(_STATE["transientCount"]) + 1
                )
                healthy = bool(_STATE["transientHealthy"])
            if healthy:
                self._send_html(200, _article_page("恢复后的正文"))
                return
            if mode == "disconnect":
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            if mode == "timeout":
                time.sleep(3)
            if mode == "503":
                self._send_html(503, "<h1>temporarily unavailable</h1>")
                return
            self._send_html(200, _article_page("恢复后的正文"))
        elif path == "/alternate":
            with _LOCK:
                _STATE["alternateCount"] = int(_STATE["alternateCount"]) + 1
            self._send_html(200, _article_page("不应访问的正文"))
        elif path == "/__stats":
            with _LOCK:
                self._send_json(200, dict(_STATE))
        elif path == "/__mutate":
            with _LOCK:
                _STATE["mutated"] = True
            self._send_json(200, {"mutated": True})
        elif path == "/__set-article":
            # 修改文章正文，模拟“发布后页面内容改变”，第二次读取应得到新正文。
            new_body = parse_qs(urlparse(self.path).query).get("body", ["新正文"])[0]
            with _LOCK:
                _STATE["articleBody"] = new_body
            self._send_json(200, {"articleBody": new_body})
        elif path == "/__set-transient":
            healthy = parse_qs(urlparse(self.path).query).get(
                "healthy", ["false"]
            )[0].lower() == "true"
            with _LOCK:
                _STATE["transientHealthy"] = healthy
            self._send_json(200, {"transientHealthy": healthy})
        else:
            self._send_html(404, "<h1>not found</h1>")

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path != "/submit":
            self._send_html(404, "<h1>not found</h1>")
            return
        length = int(self.headers.get("content-length", 0))
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        fields = {key: value[0] for key, value in parse_qs(raw).items()}
        with _LOCK:
            _STATE["submitCount"] = int(_STATE["submitCount"]) + 1  # type: ignore[arg-type]
            _STATE["lastSubmission"] = fields
        self._send_html(200, _result_page(self.business_id, fields.get("amount", "")))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8975)
    parser.add_argument("--business-id", default="EXP-DEMO-001")
    args = parser.parse_args()
    _Handler.business_id = args.business_id
    server = ThreadingHTTPServer(("127.0.0.1", args.port), _Handler)
    print(f"form site listening on http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
