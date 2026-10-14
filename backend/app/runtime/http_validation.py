"""Exact HTTP fixtures for isolated validation; never enables network access."""
import io
import json
import subprocess
import sys
import tempfile
from email.message import Message
from urllib.error import HTTPError
from urllib.request import Request


def prepare_fixture(fixture):
    """Derive expected data in an independent interpreter, never from the request.

    In local validation this child inherits the existing OS sandbox, cwd and
    isolated environment. Only JSON data crosses this boundary, not candidate
    Python objects or modules. Method, URL and status remain fixed.
    """
    if not fixture.get("prepare", "").strip():
        return fixture
    runner = '''import json, resource, sys
from contextlib import redirect_stdout
resource.setrlimit(resource.RLIMIT_FSIZE, (1048576, 1048576))
payload = json.load(sys.stdin)
scope = {"requestJson": payload["requestJson"], "responseJson": payload["responseJson"]}
with redirect_stdout(sys.stderr):
    exec(payload["prepare"], scope)
print(json.dumps({key: scope[key] for key in ("requestJson", "responseJson")}, ensure_ascii=False))
'''
    payload = {key: fixture.get(key) for key in ("requestJson", "responseJson", "prepare")}
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            process = subprocess.run([sys.executable, "-I", "-c", runner],
                input=json.dumps(payload).encode(), stdout=out, stderr=err,
                timeout=5, check=False)
        except subprocess.TimeoutExpired as exc:
            raise ValueError("HTTP fixture prepare timed out") from exc
        if process.returncode:
            err.seek(0)
            detail = err.read(4096).decode(errors="replace")
            raise ValueError("HTTP fixture prepare failed: " + detail[-1000:])
        out.seek(0)
        raw = out.read(1048577)
        if len(raw) > 1048576:
            raise ValueError("HTTP fixture prepare output exceeds 1 MiB")
    prepared = json.loads(raw)
    return {**fixture, **prepared}


class HttpFixtures:
    def __init__(self, fixtures, *, allow_prepare=False):
        self.fixtures = fixtures
        self.allow_prepare = allow_prepare
        self.consumed = 0
        self.violations = []
        self.requests = []

    def urlopen(self, request, data=None, timeout=None, **kwargs):
        try:
            return self._urlopen(request, data, timeout, **kwargs)
        except HTTPError:
            raise
        except Exception as exc:
            self.violations.append(str(exc))
            raise

    def _urlopen(self, request, data=None, timeout=None, **kwargs):
        request = request if isinstance(request, Request) else Request(request, data=data)
        if data is not None:
            request.data = data
        body = request.data
        if self.consumed >= len(self.fixtures):
            raise AssertionError("HTTP fixture missing: " + request.get_method() + " " + request.full_url)
        fixture = self.fixtures[self.consumed]
        if fixture.get("prepare", "").strip() and not self.allow_prepare:
            raise ValueError("HTTP fixture prepare requires OS-isolated local validation")
        fixture = prepare_fixture(fixture)
        actual_body = None if body is None else json.loads(body.decode("utf-8"))
        actual = (request.get_method(), request.full_url, actual_body)
        expected = (fixture["method"], fixture["url"], fixture.get("requestJson"))
        if actual != expected:
            raise AssertionError(f"HTTP fixture request mismatch: {actual!r} != {expected!r}")
        self.consumed += 1
        self.requests.append({"method": actual[0], "url": actual[1], "requestJson": actual_body, "status": fixture.get("status", 200)})
        headers = Message()
        headers["Content-Type"] = "application/json"
        payload = json.dumps(fixture.get("responseJson"), ensure_ascii=False).encode()
        status = fixture.get("status", 200)
        if status >= 400:
            raise HTTPError(request.full_url, status, "fixture response", headers, io.BytesIO(payload))
        response = io.BytesIO(payload)
        response.status = response.code = status
        response.headers = headers
        response.url = request.full_url
        response.getcode = lambda: status
        response.geturl = lambda: request.full_url
        response.info = lambda: headers
        return response

    def assert_consumed(self):
        if self.consumed != len(self.fixtures):
            raise ValueError(f"HTTP fixtures not consumed: {self.consumed}/{len(self.fixtures)}; returning request parameters is not executing HTTP")
