"""Validate nondeterministic workflow outputs with Agent-authored pure assertions."""
import ast
import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory

from app.runtime import pure_python


def _differences(expected, actual, *, partial, path="$", limit=8):
    result = []
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key, value in expected.items():
            child = path + "." + key
            if key not in actual:
                result.append(child + ": missing")
            else:
                result.extend(_differences(value, actual[key], partial=partial, path=child))
            if len(result) >= limit:
                break
        if not partial:
            result.extend(path + "." + key + ": unexpected field" for key in actual if key not in expected)
    elif isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            result.append(path + ": array length differs")
        for index, (value, observed) in enumerate(zip(expected, actual)):
            result.extend(_differences(value, observed, partial=partial, path=f"{path}[{index}]"))
            if len(result) >= limit:
                break
    elif expected != actual:
        # Paths and value types are sufficient to locate contract drift without
        # echoing source task data, credentials, or full model prose to repair.
        result.append(f"{path}: expected {type(expected).__name__}, actual {type(actual).__name__}, value differs")
    return result[:limit]


def check_expected(expected, actual, *, partial=False):
    differences = _differences(expected, actual, partial=partial)
    if differences:
        raise ValueError("工作流输出不符合业务断言：" + "; ".join(differences)[:1200])


async def verify_pure_output(example, actual, inputs):
    """Assertions see JSON values only, under the production pure-Python boundary."""
    source = example.verify
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ValueError(f"workflowExamples.verify 第 {exc.lineno} 行语法错误：{exc.msg}") from exc
    if not any(isinstance(node, ast.Assert) and any(
            isinstance(child, ast.Name) and child.id == "result" for child in ast.walk(node.test))
            for node in ast.walk(tree)):
        raise ValueError("workflowExamples.verify 必须包含针对 result 的业务 assert，不能仅检查 Schema 或 assert True")
    runner = '''import json, sys
from pure_guard import install_guard
payload = json.load(sys.stdin)
code = compile(payload["verify"], "workflowExamples.verify", "exec")
scope = {"result": payload["result"], "inputs": payload["inputs"], "expected": payload["expected"], "error": None}
install_guard()
try:
    exec(code, scope)
except BaseException as error:
    trace = error.__traceback__
    line = None
    while trace:
        if trace.tb_frame.f_code.co_filename == "workflowExamples.verify":
            line = trace.tb_lineno
        trace = trace.tb_next
    print(json.dumps({"passed": False, "type": type(error).__name__, "line": line}))
else:
    print(json.dumps({"passed": True}))
'''
    with TemporaryDirectory(prefix="skillx-workflow-assertions-") as directory:
        root = Path(directory).resolve()
        shutil.copyfile(pure_python.__file__, root / "pure_guard.py")
        (root / "runner.py").write_text(runner)
        # -I omits cwd from imports; explicitly supply only the isolated code root.
        entry = "import sys;sys.path.insert(0," + repr(str(root)) + ");exec(compile(open(" + repr(str(root / "runner.py")) + ").read(),'runner.py','exec'))"
        process = await asyncio.create_subprocess_exec(
            *pure_python.sandbox_command([sys.executable, "-I", "-c", entry], code_roots=(root,)),
            cwd=root, env={"PATH": os.defpath, "HOME": str(root), "PYTHONNOUSERSITE": "1"},
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            async with asyncio.timeout(30):
                stdout, _ = await process.communicate(json.dumps({"verify": source, "result": actual,
                    "inputs": inputs, "expected": example.expected}).encode())
            if process.returncode:
                raise ValueError("workflowExamples.verify 隔离执行失败")
            try:
                outcome = json.loads(stdout)
            except ValueError as exc:
                raise ValueError("workflowExamples.verify 不得打印或覆盖验证输出") from exc
            if not outcome.get("passed"):
                raise ValueError(f"workflowExamples.verify 第 {outcome.get('line')} 行失败：{outcome.get('type')}")
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
