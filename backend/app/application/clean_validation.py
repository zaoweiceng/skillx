"""Rebuild the pure-Python publication profile in a fresh, credential-free venv."""

import asyncio
import hashlib
import json
import os
import shutil
import sys
from uuid import uuid4

from jsonschema import Draft202012Validator

from app.config import get_settings

_RUNNER = """import asyncio, importlib.util, inspect, json, sys
from types import SimpleNamespace
spec = importlib.util.spec_from_file_location("candidate", "execute.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
inputs = json.loads(sys.stdin.read())
class Capabilities:
    async def call(self, alias, arguments):
        fixtures = json.load(open("fixtures.json"))
        matches = [f for f in fixtures if f["alias"] == alias and f["input"] == arguments]
        if not matches or any(f["output"] != matches[0]["output"] for f in matches):
            raise ValueError("No exact observed dependency fixture")
        return matches[0]["output"]
request = SimpleNamespace(input=inputs, invocation_id="clean-validation")
result = module.execute(request, SimpleNamespace(secrets={}, capabilities=Capabilities()))
if inspect.isawaitable(result):
    result = asyncio.run(result)
print(json.dumps(result))
"""


async def validate_clean_package(package, examples, output_schema, *, fixtures=()):
    root = get_settings().artifact_root.resolve() / "validation" / str(uuid4())
    root.mkdir(parents=True)
    shutil.copyfile(package / "execute.py", root / "execute.py")
    (root / "runner.py").write_text(_RUNNER)
    (root / "fixtures.json").write_text(json.dumps(fixtures))
    lock = "# Pure Python profile: standard library only; no third-party distributions.\n"
    (package / "requirements.lock").write_text(lock)
    shutil.copyfile(package / "requirements.lock", root / "requirements.lock")
    env = {"PATH": os.defpath, "HOME": str(root), "PYTHONNOUSERSITE": "1"}

    async def run(argv, data=None):
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=root,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(30):
                out, err = await process.communicate(data)
            if process.returncode:
                raise ValueError("干净环境验证失败: " + err.decode(errors="replace")[-1000:])
            return out
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    await run([sys.executable, "-m", "venv", "--without-pip", str(root / ".venv")])
    for example in examples:
        out = await run(
            [str(root / ".venv/bin/python"), "-I", "runner.py"], json.dumps(example.input).encode()
        )
        result = json.loads(out)
        Draft202012Validator(output_schema).validate(result)
        if result != example.expected:
            raise ValueError("干净环境输出与业务断言不一致")
    proof = {
        "profile": "pure-python",
        "python": sys.version,
        "examples": len(examples),
        "fixtureDigest": hashlib.sha256(json.dumps(fixtures).encode()).hexdigest(),
        "sourceDigest": hashlib.sha256((root / "execute.py").read_bytes()).hexdigest(),
        "lockDigest": hashlib.sha256(lock.encode()).hexdigest(),
        "status": "passed",
    }
    (root / "proof.json").write_text(json.dumps(proof))
    (package / "validation.json").write_text(json.dumps(proof))
