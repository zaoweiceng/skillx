from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SKILLX_", env_file=".env")

    database_url: str = "sqlite+aiosqlite:///./skillx.db"
    artifact_root: Path = Path("./artifacts")
    capability_root: Path = Path("../capabilities/skillx")
    cors_origins: list[str] = ["http://localhost:5173"]
    worker_id: str = "worker-local"
    worker_poll_seconds: float = 0.5
    lease_seconds: int = 30
    auto_create_schema: bool = False
    mcp_allowed_executables: list[str] = [
        "python",
        "python3",
        "python3.12",
        "python3.13",
    ]
    model_endpoint: str | None = None
    model_name: str | None = None
    model_api_key_env: str = "SKILLX_MODEL_API_KEY"
    traex_command: list[str] | None = None
    traex_allowed_executables: list[str] = ["traex"]


@lru_cache
def get_settings() -> Settings:
    return Settings()
