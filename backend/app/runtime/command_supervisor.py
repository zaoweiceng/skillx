"""Detached, credential-free command supervisor; independent of Worker lifetime."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def atomic(path, payload):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as stream:
        json.dump(payload, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def tail(path, limit):
    if not path.exists():
        return ""
    with path.open("rb") as stream:
        stream.seek(max(0, path.stat().st_size - limit))
        return stream.read(limit).decode(errors="replace")


def main():
    request_path = Path(sys.argv[1]).resolve()
    request = json.loads(request_path.read_text())
    prefix = request_path.with_suffix("")
    result_path = prefix.with_suffix(".result.json")
    atomic(
        prefix.with_suffix(".supervisor.json"),
        {
            "pid": os.getpid(),
            "request": str(request_path),
            "digest": request["digest"],
            "heartbeatAt": time.time(),
        },
    )
    out_path, err_path = prefix.with_suffix(".stdout"), prefix.with_suffix(".stderr")
    process = None
    status, error = "failed", None
    try:
        with out_path.open("wb") as out, err_path.open("wb") as err:
            process = subprocess.Popen(
                request["argv"],
                cwd=request["cwd"],
                env=request["env"],
                stdout=out,
                stderr=err,
                start_new_session=True,
            )
            atomic(
                prefix.with_suffix(".process.json"),
                {"pid": process.pid, "argv": request["argv"], "supervisorPid": os.getpid()},
            )
            deadline = time.monotonic() + request["timeout"]
            last_heartbeat = 0.0
            while process.poll() is None:
                if time.monotonic() - last_heartbeat > 1:
                    atomic(
                        prefix.with_suffix(".supervisor.json"),
                        {
                            "pid": os.getpid(),
                            "request": str(request_path),
                            "digest": request["digest"],
                            "heartbeatAt": time.time(),
                        },
                    )
                    last_heartbeat = time.monotonic()
                if (request_path.parent / "cancel.flag").exists():
                    status, error = "cancelled", "执行已取消"
                    break
                if time.monotonic() >= deadline:
                    error = "执行超时"
                    break
                if out_path.stat().st_size + err_path.stat().st_size > 8 * 1024 * 1024:
                    error = "执行日志超过 8 MiB"
                    break
                time.sleep(0.1)
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            elif process.returncode == 0:
                status = "succeeded"
            else:
                error = f"命令退出码 {process.returncode}"
    except Exception as exc:
        error = str(exc)
    atomic(
        result_path,
        {
            "status": status,
            "error": error,
            "exitCode": process.returncode if process else None,
            "argv": request["argv"],
            "requestDigest": request["digest"],
            "stdout": tail(out_path, 32000),
            "stderr": tail(err_path, 16000),
        },
    )


if __name__ == "__main__":
    main()
