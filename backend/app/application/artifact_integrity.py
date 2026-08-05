from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


class ArtifactPathError(ValueError):
    pass


def safe_artifact_path(
    root: Path,
    key: str,
    *,
    forbidden_root: Path | None = None,
) -> Path:
    root = root.resolve()
    if not isinstance(key, str) or not key:
        raise ArtifactPathError("Artifact key 必须是非空字符串")
    path = (root / key).resolve()
    if path == root or not path.is_relative_to(root):
        raise ArtifactPathError("Artifact 路径越界")
    if forbidden_root is not None and path.is_relative_to(forbidden_root.resolve()):
        raise ArtifactPathError("Artifact 最终路径不得位于 staging")
    return path


def artifact_file_matches(path: Path, *, size: int, sha256: str) -> bool:
    if not path.is_file() or path.stat().st_size != size:
        return False
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest() == sha256


def artifact_record_matches(path: Path, artifact: Any) -> bool:
    return artifact_file_matches(
        path,
        size=int(artifact.size),
        sha256=str(artifact.sha256),
    )
