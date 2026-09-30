from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SKILLX_", env_file=".env")

    database_url: str = "sqlite+aiosqlite:///./skillx.db"
    artifact_root: Path = Path("./artifacts")
    runtime_credentials_path: Path = Path("./.runtime-credentials")
    capability_root: Path = Path("../capabilities/skillx")
    cors_origins: list[str] = ["http://localhost:5173"]
    worker_id: str = "worker-local"
    worker_poll_seconds: float = 0.5
    lease_seconds: int = 30
    auto_create_schema: bool = False
    deterministic_workflows: bool = False
    task_max_decisions: int = 20
    task_max_active_seconds: int = 1800
    task_local_execution_enabled: bool = True
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
    llm_runtime_type: Literal["openai_compatible", "unavailable"] = "unavailable"
    llm_endpoint: str | None = None
    llm_model: str | None = None
    llm_api_key_env: str = "SKILLX_LLM_API_KEY"
    agent_runtime_type: Literal[
        "pi",
        "traex_builtin",
        "traex_managed",
        "openai_sidecar",
        "fixture",
        "unavailable",
    ] = "unavailable"
    agent_builtin_enabled: bool = True
    agent_command: list[str] | None = None
    agent_allowed_executables: list[str] = ["traex"]
    agent_trusted_adapter_digests: list[str] = []
    agent_endpoint: str | None = None
    agent_model: str | None = None
    agent_api_key_env: str = "SKILLX_AGENT_API_KEY"
    provider_probe_timeout_seconds: int = 30
    provider_probe_cooldown_seconds: int = 300
    provider_probe_ttl_seconds: int = 900
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
        "workflow.catalog",
        "workflow.search",
        "workflow.describe",
        "workflow.describe_fragment",
        "workflow.slice",
        "workflow.compile_plan",
    ]
    agent_max_steps: int = 6
    # Local Demo prioritizes long-running task completion over cost control.
    # These remain finite fail-closed guards, but are intentionally far above
    # TraeX's observed ~31k fixed input context so Host policy is not the
    # practical bottleneck. Provider/model context limits may still be lower.
    agent_max_total_tokens: int = 1250000
    agent_max_input_tokens: int = 1000000
    agent_max_output_tokens: int = 250000
    agent_max_billable_tokens: int = 1250000
    provider_probe_max_input_tokens: int = 1000000
    provider_probe_max_output_tokens: int = 250000
    provider_probe_max_billable_tokens: int = 1250000
    provider_probe_estimated_input_tokens: int = 32000
    agent_max_duration_seconds: int = 120
    agent_max_event_payload_bytes: int = 65536
    agent_lease_seconds: int = 30
    agent_max_continuation_generations: int = 3
    browser_allowed_hosts: list[str] = []
    browser_preflight_timeout_seconds: int = 10
    browser_preflight_interval_seconds: int = 60
    browser_heartbeat_ttl_seconds: int = 120


@lru_cache
def get_settings() -> Settings:
    return Settings()
