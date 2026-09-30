"""Acquire the local supervisor lock, then replace this process with Bash."""

import fcntl
import os
from pathlib import Path
import sys


def main():
    state = Path(sys.argv[1])
    state.mkdir(parents=True, exist_ok=True)
    # Never unlink this file: concurrent launchers must lock the same inode.
    lock = open(state / "supervisor.lock", "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("已有启动任务或其服务仍在运行，请先停止原服务。")

    # Respect supervisors started by the old mkdir-based implementation.
    legacy = state / "lock"
    legacy.mkdir(exist_ok=True)
    pid_file = legacy / "pid"
    try:
        previous = int(pid_file.read_text().strip())
    except (FileNotFoundError, ValueError):
        previous = 0
    if previous > 0:
        try:
            os.kill(previous, 0)
        except ProcessLookupError:
            pass
        except PermissionError:
            raise SystemExit("原启动进程仍在运行且无权访问，请先停止原服务。")
        else:
            raise SystemExit(f"已有启动任务（PID {previous}），请先停止原服务。")

    pid_file.write_text(f"{os.getpid()}\n")
    os.set_inheritable(lock.fileno(), True)
    env = dict(os.environ, WORKFLOW_DEV_LOCKED="1")
    os.execvpe(sys.argv[2], sys.argv[2:], env)


if __name__ == "__main__":
    main()
