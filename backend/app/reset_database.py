"""Explicitly discard a local SQLite database and install the current schema."""

from __future__ import annotations

import argparse
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

from app.config import get_settings


def migration_config() -> Config:
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    return config


def reset_database() -> Path:
    url = make_url(get_settings().database_url)
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        raise ValueError("重置命令仅支持本地 SQLite 文件")
    path = Path(url.database).resolve()
    for target in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        target.unlink(missing_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    command.upgrade(migration_config(), "head")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="停止 API/Worker 后重建 SQLite；删除所有历史数据")
    parser.add_argument(
        "--confirm-reset", action="store_true", help="确认永久删除数据库中的所有数据"
    )
    args = parser.parse_args()
    if not args.confirm_reset:
        parser.error("需要 --confirm-reset；请先停止 API 和 Worker")
    print(f"数据库已重建：{reset_database()}")


if __name__ == "__main__":
    main()
