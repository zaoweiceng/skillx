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


async def with_reasoning(runtime, effort):
    if effort is None:
        return replace(runtime, reasoning_effort=None)
    if effort not in {"low", "medium", "high"}:
        raise ValueError("不支持的推理强度")
    if runtime.type == "traex_builtin":
        from app.runtime.traex_models import list_traex_models

        models = await list_traex_models()
        selected = next((item for item in models if item["id"] == runtime.model), None)
        if not selected or effort not in selected.get("reasoningEfforts", []):
            raise ValueError("请先选择支持调节推理强度的模型")
    elif runtime.type == "pi":
        if not (runtime.model_options.get("reasoning") or (runtime.model or "").lower().startswith(("gpt-5", "gpt-6", "o3", "o4"))):
            raise ValueError("当前 Pi 模型未声明支持推理强度")
    else:
        raise ValueError("当前执行体不支持推理强度")
    return replace(runtime, reasoning_effort=effort)


async def agent_snapshot(
    session: AsyncSession, profile_id: str | None, traex_model: str | None = None,
    reasoning_effort: str | None = None,
    model_profile_id: str | None = None, model_id: str | None = None,
) -> dict[str, Any]:
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
        runtime = agent_runtime_config()
        if traex_model:
            if runtime.type != "traex_builtin":
                raise ValueError("仅 TraeX 支持会话模型选择")
            runtime = replace(runtime, model=traex_model)
        return asdict(await with_reasoning(runtime, reasoning_effort))
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
            model=traex_model or config.get("model") or None,
            adapter_digest="sha256:" + hashlib.sha256(bridge.read_bytes()).hexdigest(),
            adapter_version="traex-builtin/v1",
            source="profile",
        )
    else:
        if traex_model:
            raise ValueError("Pi 模型请在模型服务配置中选择")
        import hashlib
        import shutil
        from pathlib import Path

        selected_profile_id = model_profile_id or config.get("modelProfileId")
        model = (
            await session.get(RuntimeProfile, selected_profile_id) if selected_profile_id
            else await session.scalar(select(RuntimeProfile).where(
                RuntimeProfile.kind == "model", RuntimeProfile.is_default.is_(True)
            ))
        )
        if model is None and not selected_profile_id:
            # A fresh install remains usable for settings/health before a key is added.
            return asdict(replace(current, type="pi", reasoning_effort=None))
        if model is None or model.kind != "model" or not model.active:
            raise ValueError("Pi 需要有效的模型配置")
        selected_model = model_id or config.get("model") or model.config_json["model"]
        options = next((entry for entry in model.config_json.get("models", [])
                        if entry["id"] == selected_model), None)
        if options is None and model.config_json.get("models"):
            raise ValueError("所选模型不属于此服务")
        options = options or {"id": selected_model}
        options = {**options, "timeoutSeconds": model.config_json.get("timeoutSeconds", 60)}
        bridge = Path(__file__).resolve().parents[3] / "bridges/pi/bridge.mjs"
        runtime = replace(
            current,
            type="pi",
            command=(shutil.which("node") or "node", str(bridge)),
            allowed_executables=("node",),
            provider="pi",
            protocol="host-ndjson",
            endpoint=model.config_json["baseUrl"],
            model=selected_model,
            model_options=options,
            api_key_env=model.config_json["apiKeyEnv"],
            adapter_version="pi-sdk-bridge/v1",
            source="profile",
            adapter_digest="sha256:" + hashlib.sha256(bridge.read_bytes()).hexdigest(),
        )
    return asdict(await with_reasoning(runtime, reasoning_effort))


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
        if node.type == "skillx":
            from app.infrastructure.database import CapabilityVersion

            capability = await session.get(
                CapabilityVersion, node.config.get("capabilityVersionId")
            )
            interface = (
                capability.manifest_json.get("interfaces", {}).get(node.config.get("interface"), {})
                if capability
                else {}
            )
            if interface.get("kind") in {"llm", "agent"}:
                node = node.model_copy(update={"type": interface["kind"]})
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
    """Install Pi once and migrate the former builtin default, preserving frozen runs."""
    from app.application.runtime_config import llm_runtime_config
    from app.infrastructure.database import Conversation

    runtime = agent_runtime_config()
    if runtime.type in {"fixture", "unavailable"}:
        return
    builtin = await session.get(RuntimeProfile, "builtin-pi")
    if builtin is None:
        await session.execute(update(RuntimeProfile).where(RuntimeProfile.kind == "agent")
                              .values(is_default=False))
        await session.execute(update(Conversation).where(
            Conversation.agent_profile_id == "builtin-traex"
        ).values(agent_profile_id=None, traex_model=None, reasoning_effort=None))
        session.add(RuntimeProfile(
            id="builtin-pi", name="Pi Agent", kind="agent", config_json={"adapter": "pi"},
            is_default=True, active=True,
        ))
    model = llm_runtime_config()
    default_model = await session.scalar(select(RuntimeProfile.id).where(
        RuntimeProfile.kind == "model", RuntimeProfile.is_default.is_(True)))
    if default_model is None and model.type == "openai_compatible" and model.endpoint and model.model:
        existing = await session.get(RuntimeProfile, "environment-model")
        if existing is None:
            session.add(RuntimeProfile(
                id="environment-model", name="环境变量模型服务", kind="model",
                config_json={"baseUrl": model.endpoint, "model": model.model,
                             "models": [{"id": model.model}], "apiKeyEnv": model.api_key_env,
                             "structuredMode": "json_schema"},
                is_default=True, active=True,
            ))
    await session.commit()
