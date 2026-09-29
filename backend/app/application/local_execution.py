"""Task-local execution. Directory/venv isolation is NOT an OS sandbox."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

from app.application.compiler import digest_json
from app.config import get_settings

_PACKAGE = re.compile(
    r"^(?:@[a-zA-Z0-9_-]+/)?[a-zA-Z0-9_.-]+(?:\[[a-zA-Z0-9_,.-]+\])?(?:(?:==|>=|<=|~=|@)[a-zA-Z0-9_.^~+*-]+)?$"
)


class LocalOutcomeUnknown(RuntimeError):
    code = "UNKNOWN_OUTCOME"


def task_directory(scope: str) -> Path:
    if not re.fullmatch(r"[a-zA-Z0-9-]{1,100}", scope):
        raise ValueError("非法任务目录标识")
    root = (get_settings().artifact_root / "task-workspaces" / scope).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


async def execute_local(
    scope: str, invocation_id: str, kind: str, args: dict, cancelled, allow_system: bool = False
):
    if not get_settings().task_local_execution_enabled:
        raise PermissionError("本机代码执行尚未授权")
    root = task_directory(scope)
    home = root / ".home"
    home.mkdir(exist_ok=True)
    evidence = root / ".executions" / invocation_id
    evidence.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": os.pathsep.join(
            [
                str(root / ".venv/bin"),
                str(root / "node_modules/.bin"),
                "/opt/homebrew/bin",
                "/usr/local/bin",
                "/usr/bin",
                "/bin",
            ]
        ),
        "HOME": str(home),
        "TMPDIR": str(root),
        "LANG": "en_US.UTF-8",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PYTHONNOUSERSITE": "1",
        "npm_config_cache": str(root / ".npm-cache"),
    }

    async def run(argv: list[str], label: str):
        request_path = evidence / (label + ".request")
        result_path = evidence / (label + ".result.json")
        request = {"argv": argv, "cwd": str(root), "env": env, "timeout": 300}
        request["digest"] = digest_json(request)
        try:
            with request_path.open("x") as stream:
                json.dump(request, stream)
                stream.flush()
                os.fsync(stream.fileno())
            supervisor = Path(__file__).resolve().parents[1] / "runtime/command_supervisor.py"
            # The command owner survives Worker shutdown. A claim is never replayed.
            await asyncio.create_subprocess_exec(
                sys.executable,
                str(supervisor),
                str(request_path),
                cwd=root,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
        except FileExistsError:
            saved = json.loads(request_path.read_text())
            if saved["digest"] != request["digest"]:
                raise ValueError("命令快照已变化，禁止重放") from None
        attached = time.monotonic()
        while not result_path.exists():
            if await cancelled():
                (evidence / "cancel.flag").touch()
            receipt = evidence / (label + ".supervisor.json")
            if receipt.exists():
                owner = json.loads(receipt.read_text())
                if time.time() - owner.get("heartbeatAt", 0) > 10:
                    raise LocalOutcomeUnknown("命令监督进程心跳失效，禁止重放")
                if owner["digest"] != request["digest"]:
                    raise ValueError("进程身份摘要不匹配")
                try:
                    os.kill(owner["pid"], 0)
                except ProcessLookupError:
                    if not result_path.exists():
                        raise LocalOutcomeUnknown("命令监督进程失联，禁止重放") from None
            elif time.monotonic() - attached > 5:
                raise LocalOutcomeUnknown("命令派发结果未知，禁止重放")
            await asyncio.sleep(0.1)
        result = json.loads(result_path.read_text())
        if result["requestDigest"] != request["digest"]:
            raise ValueError("命令结果摘要不匹配")
        if result["status"] == "cancelled":
            raise asyncio.CancelledError()
        if result["status"] != "succeeded":
            raise ValueError(f"{label}: {result['error']}: {result['stderr']}")
        return result

    if kind == "install":
        packages = args.get("packages", [])
        if (
            not isinstance(packages, list)
            or not packages
            or any(
                not isinstance(x, str) or not _PACKAGE.fullmatch(x) or x.startswith("-")
                for x in packages
            )
        ):
            raise ValueError("安装仅接受包名和版本，不接受 URL、路径或命令选项")
        manager = args.get("manager")
        if args.get("scope") == "system":
            if not allow_system:
                raise PermissionError("系统安装必须具有当前操作快照的明确授权")
            env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
            executable = shutil.which(str(manager), path=env["PATH"])
            if not executable:
                raise ValueError("系统包管理器未安装")
            if manager == "npm":
                argv = [executable, "install", "--global", "--", *packages]
            elif manager == "brew":
                argv = [executable, "install", "--", *packages]
            else:
                raise ValueError("系统安装仅支持 npm/brew；Python 默认使用任务 venv")
            if args.get("elevated") is True:
                argv = ["/usr/bin/sudo", "-n", *argv]
            env["HOME"] = os.path.expanduser("~")
            result = await run(argv, "system-install")
        elif manager == "pip":
            python = root / ".venv/bin/python"
            if not python.exists() or (evidence / "venv.request").exists():
                await run([sys.executable, "-m", "venv", str(root / ".venv")], "venv")
            result = await run([str(python), "-m", "pip", "install", *packages], "install")
            await run([str(python), "-m", "pip", "freeze", "--all"], "lock")
            (root / "requirements.lock").write_bytes((evidence / "lock.stdout").read_bytes())
        elif manager == "npm":
            result = await run(
                ["npm", "install", "--prefix", str(root), "--save-exact", "--", *packages],
                "install",
            )
        else:
            raise ValueError("仅支持 pip/npm 任务本地安装")
    else:
        language = args.get("language")
        source = args.get("source")
        if (
            language not in {"python", "javascript"}
            or not isinstance(source, str)
            or len(source) > 200000
        ):
            raise ValueError("代码必须是 python/javascript，最多 200000 字符")
        script = evidence / ("main.py" if language == "python" else "main.js")
        script.write_text(source)
        python = root / ".venv/bin/python"
        if language == "python" and (not python.exists() or (evidence / "venv.request").exists()):
            await run([sys.executable, "-m", "venv", str(root / ".venv")], "venv")
        executable = str(python)
        result = await run([executable if language == "python" else "node", str(script)], "execute")
    result["evidence"] = str(evidence.relative_to(get_settings().artifact_root.resolve()))
    result["requestDigest"] = digest_json(args)
    (evidence / "completed.json").write_text(json.dumps(result))
    return result


def cancel_local(scope: str, invocation_id: str):
    path = task_directory(scope) / ".executions" / invocation_id
    path.mkdir(parents=True, exist_ok=True)
    (path / "cancel.flag").touch()
