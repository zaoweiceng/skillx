from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


@lru_cache
def alembic_head_revision() -> str:
    """从 Alembic ScriptDirectory 动态读取当前 head，避免硬编码 revision。"""

    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    head = script.get_current_head()
    if head is None:
        raise RuntimeError("无法解析 Alembic head revision")
    return head
