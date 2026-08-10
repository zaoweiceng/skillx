from functools import lru_cache
from pathlib import Path
from typing import Literal

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
    agent_provider_type: Literal["real", "fixture", "unavailable"] = "unavailable"
    traex_command: list[str] | None = None
    traex_allowed_executables: list[str] = ["traex"]
    # Agent Host policy：Host 授予的 Tool 集合是安全边界，请求只能取交集缩小范围，
    # 不能扩大。步数/Token/时长上限由 Host 独立强制，畸形事件 fail closed。
    agent_host_capabilities: list[str] = [
        "search.web",
        "browser.read",
        "read.local",
        "lookup.table",
        "summarize.text",
        "workflow.search",
        "workflow.describe",
        "workflow.describe_fragment",
        "workflow.slice",
        "workflow.compile_plan",
    ]
    agent_max_steps: int = 6
    agent_max_total_tokens: int = 30000
    agent_max_duration_seconds: int = 120
    agent_max_event_payload_bytes: int = 65536
    agent_lease_seconds: int = 30
    agent_max_continuation_generations: int = 3
    browser_allowed_hosts: list[str] = []


@lru_cache
def get_settings() -> Settings:
    return Settings()
