"""Behavior-based boundary shared by generated pure Python validation and execution.

Imports are not effects. The OS confines reads to runtime code, and an audit hook
rejects actual business I/O/process/native calls before they happen. This module
uses only stdlib so the identical guard can run inside the clean validation venv.
"""
import json
import os
from pathlib import Path
import shutil
import sys


def execution_profile(package_root, interface):
    """Older generated packages recorded the profile in their verified proof."""
    if interface.get("x-executionProfile"):
        return interface["x-executionProfile"]
    proof = Path(package_root) / "validation.json"
    if proof.is_file():
        return json.loads(proof.read_text()).get("profile")
    return None


def sandbox_command(argv, *, code_roots=()):
    if sys.platform != "darwin" or not shutil.which("sandbox-exec"):
        raise ValueError("pure-python 执行需要 OS 读取/写入/网络隔离；当前 Host 未配置")
    executable = str(Path(argv[0]).resolve())
    roots = {str(Path(path).resolve()) for path in (
        sys.base_prefix, sys.prefix, "/System/Library", "/usr/lib", *code_roots)}
    reads = " ".join("(subpath " + json.dumps(path) + ")" for path in roots)
    # dyld/Python traverse their executable's ancestors during startup. Permit
    # directory entries themselves, never the contents of unrelated subtrees.
    ancestors = {str(parent) for path in (*roots, str(argv[0])) for parent in Path(path).parents}
    reads += " " + " ".join("(literal " + json.dumps(path) + ")" for path in ancestors)
    profile = ('(version 1) (allow default) (deny network*) (deny file-write*) '
        '(deny file-read*) (deny process-fork) (deny process-exec) '
        '(allow process-exec (literal ' + json.dumps(executable) + ') (literal ' + json.dumps(str(argv[0])) + ')) '
        '(allow file-read-metadata) (allow file-read* ' + reads + ' (subpath "/dev")) '
        '(allow file-write* (literal "/dev/null"))')
    return ["sandbox-exec", "-p", profile, *argv]


def install_guard():
    """Installed only in a disposable child; never mutate the Host process."""
    sys.dont_write_bytecode = True

    def import_loader():
        # CPython's import machinery may read module bytes. Candidate code does
        # not gain permission to read arbitrary files merely by importing os/io.
        frame = sys._getframe(2)
        return frame.f_code.co_filename.startswith("<frozen importlib.")

    def reject(event, args):
        if event == "open":
            mode, flags = args[1], args[2]
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)))
            if writing or not import_loader():
                raise PermissionError("pure-python 禁止业务文件 I/O；请使用明确的 local-command 或依赖契约")
        elif event in {"os.listdir", "os.scandir"}:
            if not import_loader():
                raise PermissionError("pure-python 禁止目录访问")
        elif (event.startswith(("socket.", "subprocess.", "ctypes."))
              or event in {"os.system", "os.exec", "os.posix_spawn", "os.fork", "os.forkpty",
                           "os.remove", "os.rename", "os.rmdir", "os.mkdir", "os.chmod", "os.chown",
                           "os.link", "os.symlink", "os.truncate", "os.chdir", "os.putenv", "os.unsetenv"}):
            raise PermissionError("pure-python 禁止外部副作用：" + event)
    sys.addaudithook(reject)
