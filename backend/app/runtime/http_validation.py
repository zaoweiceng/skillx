"""Exact HTTP fixtures for isolated validation; never enables network access."""
import io
import json
from email.message import Message
from urllib.error import HTTPError
from urllib.request import Request


class HttpFixtures:
    def __init__(self, fixtures):
        self.fixtures = fixtures
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
