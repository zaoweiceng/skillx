"""Local, owner-only credential files. Persist immutable references in run snapshots."""

import os
import re
from uuid import uuid4

from app.config import get_settings

PREFIX = "SKILLX_STORED_KEY_"


def store_runtime_key(value: str) -> str:
    root = get_settings().runtime_credentials_path.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    reference = PREFIX + uuid4().hex
    descriptor = os.open(root / reference, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(value)
    return reference


def resolve_runtime_key(reference: str) -> str | None:
    if not reference.startswith(PREFIX):
        return os.environ.get(reference)
    if not re.fullmatch(PREFIX + r"[0-9a-f]{32}", reference):
        return None
    try:
        return (get_settings().runtime_credentials_path / reference).read_text() or None
    except FileNotFoundError:
        return None


def discard_runtime_key(reference: str) -> None:
    if re.fullmatch(PREFIX + r"[0-9a-f]{32}", reference):
        (get_settings().runtime_credentials_path / reference).unlink(missing_ok=True)
