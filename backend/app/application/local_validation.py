"""Run generated command scripts in a disposable, write-confined workspace.

This is validation infrastructure, not a Git implementation. The Agent supplies
the implementation, fixture setup and independent postcondition assertions.
"""
import asyncio
import json
import os
import shutil
import signal
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from jsonschema import Draft202012Validator


class LocalValidation:
    def __init__(self):
        if sys.platform != "darwin" or not shutil.which("sandbox-exec"):
            raise ValueError("命令脚本验证需要可用的 OS 写隔离；当前环境尚未配置，禁止在真实目录验证")
        self.temp = TemporaryDirectory(prefix="skillx-command-validation-")
        self.root = Path(self.temp.name).resolve()
        self.fixture_output = {}
        self.http_fixtures = []
        self.http_requests = []
        self.env = {"PATH": os.defpath, "HOME": str(self.root), "TMPDIR": str(self.root),
            "PYTHONNOUSERSITE": "1", "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
        # Keep policy in the Host, outside the candidate's writable workspace.
        self.profile = ('(version 1) (allow default) (deny network*) '
            '(deny file-write*) (allow file-write* (literal "/dev/null") (subpath ' + json.dumps(str(self.root)) + '))')

    def close(self):
        self.temp.cleanup()

    async def python(self, source, data=None):
        script = self.root / (str(uuid4()) + ".py")
        script.write_text(source)
        process = await asyncio.create_subprocess_exec(
            "sandbox-exec", "-p", self.profile, sys.executable, "-I", str(script),
            cwd=self.root, env=self.env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        try:
            async with asyncio.timeout(30):
                out, err = await process.communicate(json.dumps(data).encode())
            if process.returncode:
                detail = err.decode(errors="replace").strip()
                lines = detail.splitlines()
                summary = lines[-1] if lines else f"进程退出码 {process.returncode}"
                raise ValueError("隔离命令验证失败：" + summary[:400] + "\n" + "\n".join(lines[:-1])[-1200:])
            return out.decode()
        finally:
            # Kill descendants as well, including processes left behind by setup.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()

    async def setup(self, source):
        if not source.strip():
            raise ValueError("命令能力必须提供临时目录 setup，不能复用真实工作目录")
        output = await self.python(source)
        self.fixture_output = {}
        try:
            parsed = json.loads(output)
        except ValueError:
            return  # Ordinary preparation logs are not a fixture protocol.
        if isinstance(parsed, dict):
            self.fixture_output = parsed

    async def resolve_inputs(self, inputs):
        """Resolve explicit fixture references after setup; never evaluate code."""
        raw = await self.python("import pathlib\np=pathlib.Path('.skillx-fixture.json')\nprint(p.read_text() if p.exists() else '{}')")
        fixture = json.loads(raw)
        if not isinstance(fixture, dict):
            raise ValueError(".skillx-fixture.json 必须是对象")
        for key, value in self.fixture_output.items():
            if key in fixture and fixture[key] != value:
                raise ValueError("测试准备输出通道冲突：" + key)
        fixture = {**self.fixture_output, **fixture}
        def resolve(value):
            if isinstance(value, str) and value.startswith("${fixture.") and value.endswith("}"):
                key = value[len("${fixture."):-1]
                if key not in fixture:
                    raise ValueError("测试准备阶段缺少动态值：" + key)
                return fixture[key]
            if isinstance(value, dict): return {k: resolve(v) for k, v in value.items()}
            if isinstance(value, list): return [resolve(v) for v in value]
            return value
        return resolve(inputs)

    async def configure_http(self, example):
        self.http_fixtures = await self.resolve_inputs([item.model_dump() for item in example.httpFixtures])
        self.http_requests = []

    async def execute(self, source, inputs, schema, expected_error=None):
        module = self.root / (str(uuid4()) + ".py")
        module.write_text(source)
        runner = '''import asyncio, importlib.util, inspect, json, sys
sys.path.insert(0, BACKEND)
from app.runtime.skillx import SkillXRequest, SkillXContext, SkillXResult
from contextlib import redirect_stdout
from app.runtime.http_validation import HttpFixtures
import urllib.request
payload = json.load(sys.stdin)
http = HttpFixtures(payload["httpFixtures"], allow_prepare=True)
urllib.request.urlopen = http.urlopen
spec = importlib.util.spec_from_file_location("candidate", MODULE)
module = importlib.util.module_from_spec(spec)
with redirect_stdout(sys.stderr):
    spec.loader.exec_module(module)
    try:
        result = module.execute(SkillXRequest(input=payload["inputs"], invocation_id="validation", interface="execute"), SkillXContext())
        if inspect.isawaitable(result): result = asyncio.run(result)
        if isinstance(result, SkillXResult): result = result.output
        outcome = {"output": result}
    except Exception as exc:
        outcome = {"error": {"type": type(exc).__name__, "message": str(exc)}}
outcome["httpConsumed"] = http.consumed
outcome["httpViolations"] = http.violations
outcome["httpRequests"] = http.requests
print(json.dumps(outcome))
'''.replace("MODULE", repr(str(module))).replace("BACKEND", repr(str(Path(__file__).resolve().parents[2])))
        outcome = json.loads(await self.python(runner, {"inputs": inputs, "httpFixtures": self.http_fixtures}))
        self.http_fixtures = self.http_fixtures[outcome["httpConsumed"]:]
        self.http_requests.extend(outcome["httpRequests"])
        if outcome["httpViolations"]:
            raise ValueError("HTTP fixture violation: " + "; ".join(outcome["httpViolations"]))
        error = outcome.get("error")
        if expected_error:
            if not error or error["type"] != expected_error.type or expected_error.messageContains not in error["message"]:
                raise ValueError(f"预期拒绝未发生或错误不匹配：{error}")
            return {"validationError": error}
        if error:
            raise ValueError(f"候选执行失败：{error['type']}: {error['message']}")
        result = outcome["output"]
        Draft202012Validator(schema).validate(result)
        return result

    async def verify(self, example, result, inputs=None):
        if self.http_fixtures:
            raise ValueError("HTTP fixtures not consumed; candidate must execute every declared request")
        if not example.verify.strip() or "assert" not in example.verify:
            raise ValueError("命令能力必须提供独立的仓库/文件后置条件 assert")
        negative = getattr(example, "expectedError", None)
        if inputs is None:
            inputs = await self.resolve_inputs(example.input)
        expected = await self.resolve_inputs(example.expected)
        if not negative:
            from app.application.workflow_assertions import check_expected
            check_expected(expected, result, partial=True)
            if getattr(example, "failureOutput", None):
                check_expected(example.failureOutput, result, partial=True)
        payload = {"result": None if negative else result, "inputs": inputs, "expected": expected,
                   "error": result["validationError"] if negative else None, "httpRequests": self.http_requests}
        await self.python("import json, sys\n_payload = json.load(sys.stdin)\nresult = _payload['result']\nerror = _payload['error']\ninputs = _payload['inputs']\nexpected = _payload['expected']\nhttpRequests = _payload['httpRequests']\n" + example.verify, payload)


async def validate_local_package(package, examples, output_schema):
    observations = []
    successful_inputs = []
    for example in examples:
        workspace = LocalValidation()
        try:
            await workspace.setup(example.setup)
            inputs = await workspace.resolve_inputs(example.input)
            await workspace.configure_http(example)
            input_file = package / "input.json"
            if input_file.exists():
                Draft202012Validator(json.loads(input_file.read_text())).validate(inputs)
            result = await workspace.execute((package / "execute.py").read_text(), inputs, output_schema,
                getattr(example, "expectedError", None))
            await workspace.verify(example, result, inputs)
            observations.append({"input": inputs, "output": result})
            if not getattr(example, "expectedError", None) and not getattr(example, "failureOutput", None):
                successful_inputs.append(inputs)
        finally:
            workspace.close()
    if (package / "input.json").exists():
        from app.application.input_form_coverage import validate_input_form_coverage
        validate_input_form_coverage(json.loads((package / "input.json").read_text()),
                                    successful_inputs)
    proof = {"profile": "local-command", "status": "passed", "examples": len(examples),
        "isolation": "os-write-confined-no-network", "observations": observations}
    (package / "validation.json").write_text(json.dumps(proof))
