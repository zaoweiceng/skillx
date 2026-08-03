import os
import shutil
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
import yaml

from app.application.capabilities import register_skillx, resolve_skillx_node
from app.application.secrets import register_secret_reference
from app.config import get_settings
from app.domain.workflow import WorkflowNode
from app.infrastructure.database import SessionLocal
from app.runtime.skillx_host import execute_in_subprocess


@pytest_asyncio.fixture
async def secret_capability(tmp_path: Path) -> AsyncIterator[tuple[Path, str]]:
    source = Path(__file__).resolve().parents[2] / "capabilities" / "skillx" / "text-stats"
    root = tmp_path / "capabilities"
    package = root / "secret-reader"
    shutil.copytree(source, package)
    manifest_path = package / "skillx.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["metadata"]["name"] = "secret-reader"
    manifest["spec"]["permissions"]["secrets"]["use"] = ["demo-api-key"]
    manifest["spec"]["interfaces"]["count"]["permissions"]["secrets"] = {
        "use": ["demo-api-key"]
    }
    manifest_path.write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    skill = package / "SKILL.md"
    skill.write_text(
        skill.read_text(encoding="utf-8").replace("name: text-stats", "name: secret-reader"),
        encoding="utf-8",
    )
    executor = package / "executors" / "count.py"
    executor.write_text(
        """
async def execute(request, context):
    value = context.secrets.get("demo-api-key")
    return {
        "characters": len(value),
        "nonWhitespaceCharacters": len(value),
        "words": 1,
    }
""".lstrip(),
        encoding="utf-8",
    )
    settings = get_settings()
    previous = settings.capability_root
    settings.capability_root = root
    os.environ["SKILLX_TEST_DEMO_SECRET"] = "do-not-persist"
    try:
        yield package, "do-not-persist"
    finally:
        settings.capability_root = previous
        os.environ.pop("SKILLX_TEST_DEMO_SECRET", None)


async def test_worker_resolves_only_manifest_declared_secret_handle(
    secret_capability: tuple[Path, str],
) -> None:
    _, secret_value = secret_capability
    async with SessionLocal() as session, session.begin():
        await register_secret_reference(
            session,
            "demo-api-key",
            "SKILLX_TEST_DEMO_SECRET",
        )
        _, version = await register_skillx(session, "secret-reader")
        node = WorkflowNode.model_validate(
            {
                "id": "read",
                "type": "skillx",
                "displayName": "读取 Secret",
                "config": {
                    "capabilityVersionId": version.id,
                    "interface": "count",
                },
            }
        )
        resolved = await resolve_skillx_node(session, node)

    assert resolved.config["_resolvedSecrets"] == {"demo-api-key": secret_value}
    assert secret_value not in str(version.manifest_json)


async def test_skillx_denies_undeclared_secret_and_blocks_secret_echo(
    secret_capability: tuple[Path, str],
) -> None:
    package, secret_value = secret_capability
    denied = await execute_in_subprocess(
        package,
        "count",
        "invocation-denied",
        {"text": "hello"},
        secrets={},
    )
    assert denied.status == "failed"
    assert denied.error is not None
    assert denied.error.code == "PERMISSION_DENIED"

    echoed = await execute_in_subprocess(
        package,
        "count",
        "invocation-allowed",
        {"text": "hello"},
        secrets={"demo-api-key": secret_value},
    )
    assert echoed.status == "succeeded"
    assert secret_value not in echoed.model_dump_json()


async def test_mcp_registration_rejects_plaintext_environment_secret() -> None:
    from app.application.mcp import (
        MCPConfigurationError,
        register_server,
        resolve_server_config,
    )

    async with SessionLocal() as session:
        with pytest.raises(MCPConfigurationError):
            await register_server(
                session,
                "unsafe-server",
                "stdio",
                {
                    "command": ["python", "-c", "pass"],
                    "environment": {"API_KEY": "plaintext"},
                },
            )

    os.environ["SKILLX_TEST_MCP_SECRET"] = "mcp-secret-value"
    try:
        async with SessionLocal() as session, session.begin():
            await register_secret_reference(
                session,
                "mcp-api-key",
                "SKILLX_TEST_MCP_SECRET",
            )
            _, version = await register_server(
                session,
                "safe-server",
                "stdio",
                {
                    "command": ["python", "-c", "pass"],
                    "environment": {"API_KEY": {"secretRef": "mcp-api-key"}},
                },
            )
            resolved, values = await resolve_server_config(
                session,
                version.config_json,
            )
            assert resolved["environment"] == {"API_KEY": "mcp-secret-value"}
            assert values == ["mcp-secret-value"]
            assert "mcp-secret-value" not in str(version.config_json)
    finally:
        os.environ.pop("SKILLX_TEST_MCP_SECRET", None)
