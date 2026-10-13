import json
import subprocess
import sys
from pathlib import Path

from app.application.task_context import call_context
from app.runtime.command_supervisor import captured_stream


def test_supervisor_keeps_multibyte_document_head_and_tail(tmp_path):
    # The previous 32,000-byte tail silently removed the opening contract.
    text = "FIRST\n" + "中文证据\n" * 3000 + "LAST\n"
    request = tmp_path / "read.request"
    request.write_text(json.dumps({
        "argv": [sys.executable, "-c", f"print({text!r}, end='')"],
        "cwd": str(tmp_path), "env": {}, "timeout": 10, "digest": "fixture",
    }))
    supervisor = Path(__file__).parents[1] / "app/runtime/command_supervisor.py"
    subprocess.run([sys.executable, str(supervisor), str(request)], check=True, timeout=15)
    result = json.loads((tmp_path / "read.result.json").read_text())
    assert result["status"] == "succeeded"
    assert result["stdout"] == text
    assert not result["streams"]["stdout"]["truncated"]
    records, _ = call_context([{
        "id": "read", "kind": "code", "request": {}, "error": None, "output": result,
    }], tmp_path / "context", limit=64000)
    assert records[0]["output"]["stdout"] == text
    assert records[0]["output"]["streams"]["stdout"]["totalBytes"] == len(text.encode())


def test_large_capture_exposes_exact_record_and_omission(tmp_path):
    path = tmp_path / "stdout"
    path.write_bytes(b"first" + b"x" * 300000 + b"last")
    preview, info = captured_stream(path, 256 * 1024)
    assert preview.startswith("first")
    assert info["truncated"] and info["offsetBytes"] == 0
    assert info["retainedBytes"] == 256 * 1024
    assert Path(info["path"]).read_bytes().endswith(b"last")
    preview, info = captured_stream(path, 16000, keep_tail=True)
    assert preview.endswith("last")
    assert info["offsetBytes"] + info["retainedBytes"] == info["totalBytes"]
