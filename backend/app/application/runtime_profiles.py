"""Persisted execution choices; snapshots contain references, never credentials."""

from dataclasses import asdict, replace
from typing import Any
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.runtime_config import AgentRuntimeConfig, agent_runtime_config
from app.infrastructure.database import RuntimeProfile


async def list_profiles(session: AsyncSession) -> list[RuntimeProfile]:
    return list((await session.scalars(select(RuntimeProfile).order_by(RuntimeProfile.name))).all())


async def save_profile(
    session: AsyncSession,
    *,
    profile_id: str | None,
    name: str,
    kind: str,
    config: dict[str, Any],
    is_default: bool,
    active: bool,
) -> RuntimeProfile:
    item = await session.get(RuntimeProfile, profile_id) if profile_id else None
    if profile_id and item is None:
        raise LookupError("配置不存在")
    if item is not None and item.kind != kind:
        raise ValueError("不能改变配置类型")
    if is_default and not active:
        raise ValueError("默认配置不能停用")
    if is_default and kind == "agent" and config.get("adapter") == "pi":
        probe = item.probe_json if item is not None else None
        if (
            item is None
            or item.config_json != config
            or not probe
            or probe.get("status") != "available"
            or probe.get("runtimeDigest")
            != restore_agent_snapshot(await agent_snapshot(session, item.id)).digest
        ):
            raise ValueError("Pi 必须按当前模型配置探测成功后才能设为默认")
    if is_default:
        await session.execute(
            update(RuntimeProfile).where(RuntimeProfile.kind == kind).values(is_default=False)
        )
    if item is None:
        item = RuntimeProfile(id=str(uuid4()), kind=kind)
        session.add(item)
    if item.config_json != config:
        item.probe_json = None
    item.name, item.config_json = name, config
    item.is_default, item.active = is_default, active
    await session.flush()
    return item


async def agent_snapshot(session: AsyncSession, profile_id: str | None) -> dict[str, Any]:
    profile = (
        await session.get(RuntimeProfile, profile_id)
        if profile_id
        else await session.scalar(
            select(RuntimeProfile).where(
                RuntimeProfile.kind == "agent", RuntimeProfile.is_default.is_(True)
            )
        )
    )
    if profile is None:
        if profile_id:
            raise ValueError("Agent 配置不存在")
        return asdict(agent_runtime_config())
    if not profile.active or profile.kind != "agent":
        raise ValueError("Agent 配置不可用")
    config = profile.config_json
    current = agent_runtime_config()
    adapter = config["adapter"]
    if adapter == "traex":
        import hashlib
        import shutil
        from pathlib import Path

        command = shutil.which("traex")
        if not command:
            raise ValueError("本机未安装 TraeX")
        bridge = Path(__file__).resolve().parents[1] / "runtime/traex_builtin.py"
        runtime = replace(
            current,
            type="traex_builtin",
            command=(command,),
            allowed_executables=(Path(command).name,),
            provider="traex",
            protocol="traex-cli-jsonl+mcp",
            endpoint=None,
            model=None,
            adapter_digest="sha256:" + hashlib.sha256(bridge.read_bytes()).hexdigest(),
            adapter_version="traex-builtin/v1",
            source="profile",
        )
    else:
        import hashlib
        import shutil
        from pathlib import Path

        model = await session.get(RuntimeProfile, config.get("modelProfileId"))
        if model is None or model.kind != "model" or not model.active:
            raise ValueError("Pi 需要有效的模型配置")
        bridge = Path(__file__).resolve().parents[3] / "bridges/pi/bridge.mjs"
        runtime = replace(
            current,
            type="pi",
            command=(shutil.which("node") or "node", str(bridge)),
            allowed_executables=("node",),
            provider="pi",
            protocol="host-ndjson",
            endpoint=model.config_json["baseUrl"],
            model=model.config_json["model"],
            api_key_env=model.config_json["apiKeyEnv"],
            adapter_version="pi-sdk-bridge/v1",
            source="profile",
            adapter_digest="sha256:" + hashlib.sha256(bridge.read_bytes()).hexdigest(),
        )
    return asdict(runtime)


def restore_agent_snapshot(snapshot: dict[str, Any] | None) -> AgentRuntimeConfig | None:
    if not snapshot:
        return None
    return AgentRuntimeConfig(
        **{
            **snapshot,
            "command": tuple(snapshot["command"]),
            "allowed_executables": tuple(snapshot["allowed_executables"]),
        }
    )


async def workflow_profiles(session: AsyncSession, definition: Any) -> dict[str, Any]:
    from app.application.compiler import digest_json
    from app.application.runtime_config import LLMRuntimeConfig

    result = {}
    default_model = await session.scalar(
        select(RuntimeProfile).where(
            RuntimeProfile.kind == "model", RuntimeProfile.is_default.is_(True)
        )
    )
    for node in definition.spec.nodes:
        if node.type == "agent" and node.config.get("agentProfile") != "fixture":
            result[node.id] = {
                "agentConfig": await agent_snapshot(session, node.config.get("agentProfileId"))
            }
        elif node.type == "llm" and node.config.get("modelProfile") != "fake":
            profile_id = node.config.get("modelProfileId")
            profile = await session.get(RuntimeProfile, profile_id) if profile_id else default_model
            if profile is None:
                if profile_id:
                    raise ValueError("模型配置不存在")
                continue
            if not profile.active or profile.kind != "model":
                raise ValueError("模型配置不可用")
            config = profile.config_json
            runtime = LLMRuntimeConfig(
                "openai_compatible", config["baseUrl"], config["model"], config["apiKeyEnv"]
            )
            result[node.id] = {
                "modelConfig": asdict(runtime),
                "structuredMode": config.get("structuredMode"),
                "timeoutSeconds": config.get("timeoutSeconds", 60),
                "runtimeSelection": "direct_llm",
                "runtimeKind": "llm_runtime",
                "provider": "openai-compatible",
                "identity": runtime.model,
                "protocol": "openai-compatible",
                "runtimeType": runtime.type,
                "runtimeDigest": runtime.digest,
                "policyDigest": digest_json({"profileId": profile.id}),
            }
    return result


async def initialize_profiles(session: AsyncSession) -> None:
    """Import environment defaults once; never replace saved user choices."""
    from app.application.runtime_config import llm_runtime_config

    if await session.scalar(select(RuntimeProfile.id).limit(1)) is not None:
        return
    runtime = agent_runtime_config()
    if runtime.type == "fixture":
        return
    if runtime.type == "traex_builtin":
        session.add(
            RuntimeProfile(
                id="builtin-traex",
                name="TraeX",
                kind="agent",
                config_json={"adapter": "traex"},
                is_default=True,
                active=True,
            )
        )
    model = llm_runtime_config()
    if model.type == "openai_compatible" and model.endpoint and model.model:
        session.add(
            RuntimeProfile(
                id="environment-model",
                name="环境变量模型服务",
                kind="model",
                config_json={
                    "baseUrl": model.endpoint,
                    "model": model.model,
                    "apiKeyEnv": model.api_key_env,
                    "structuredMode": "json_schema",
                },
                is_default=True,
                active=True,
            )
        )
    await session.commit()
