"""Configuration endpoints independent from workflow business routes."""

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.runtime_profiles import agent_snapshot, list_profiles, save_profile
from app.infrastructure.database import Conversation, RuntimeProfile, get_session
from app.infrastructure.runtime_credentials import (
    discard_runtime_key,
    resolve_runtime_key,
    store_runtime_key,
)
from app.runtime.traex_models import list_traex_models, validate_traex_model

router = APIRouter(prefix="/runtime-profiles", tags=["Runtime profiles"])


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=200, pattern=r"^\S+$")
    reasoning: bool = False
    images: bool = False
    contextWindow: int = Field(default=128000, ge=1024, le=10000000)
    maxOutputTokens: int = Field(default=8192, ge=1, le=1000000)

    @model_validator(mode="after")
    def validate_limits(self):
        if self.maxOutputTokens > self.contextWindow:
            raise ValueError("最大输出 Token 不能超过上下文窗口")
        return self


class ProfileConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    adapter: Literal["traex", "pi"] | None = None
    modelProfileId: str | None = None
    baseUrl: str | None = Field(default=None, pattern=r"^https?://[^\s]+$")
    model: str | None = Field(default=None, min_length=1, max_length=200)
    models: list[ModelConfig] | None = Field(default=None, min_length=1, max_length=100)
    apiKey: SecretStr | None = Field(default=None, json_schema_extra={"writeOnly": True})
    timeoutSeconds: float = Field(default=60, ge=1, le=600)
    apiKeyEnv: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    structuredMode: Literal["json_schema", "json_object", "prompt"] = "json_schema"


class ProfileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    kind: Literal["model", "agent"]
    config: ProfileConfig
    isDefault: bool = False
    active: bool = True

    @model_validator(mode="after")
    def validate_kind(self):
        if self.kind == "model":
            config = self.config
            if not config.baseUrl or not (config.model or config.models):
                raise ValueError("模型服务需要 Base URL 和至少一个模型")
            if config.models:
                ids = [model.id for model in config.models]
                if len(ids) != len(set(ids)):
                    raise ValueError("模型 ID 不能重复")
                config.model = config.model or ids[0]
                if config.model not in ids:
                    raise ValueError("默认模型必须属于此服务")
        elif not self.config.adapter:
            raise ValueError("Agent 需要执行体")
        if self.kind == "agent" and self.config.apiKey is not None:
            raise ValueError("请在模型服务中配置密钥")
        return self


def view(item: RuntimeProfile) -> dict[str, Any]:
    return {
        "id": item.id,
        "name": item.name,
        "kind": item.kind,
        "config": item.config_json,
        "isDefault": item.is_default,
        "active": item.active,
        "probe": item.probe_json,
        "apiKeyConfigured": bool(resolve_runtime_key(item.config_json.get("apiKeyEnv", ""))),
    }


@router.get("")
async def listing(session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    return [view(item) for item in await list_profiles(session)]


async def persist(body: ProfileInput, session: AsyncSession, profile_id: str | None = None):
    new_reference = None
    try:
        if body.kind == "agent" and body.config.adapter == "traex":
            await validate_traex_model(body.config.model)
        config = body.config.model_dump(exclude_none=True, exclude={"apiKey"})
        if body.kind == "model":
            existing = await session.get(RuntimeProfile, profile_id) if profile_id else None
            key = body.config.apiKey.get_secret_value() if body.config.apiKey else ""
            if key:
                new_reference = store_runtime_key(key)
                config["apiKeyEnv"] = new_reference
            elif existing and existing.kind == "model":
                config["apiKeyEnv"] = config.get("apiKeyEnv") or existing.config_json.get(
                    "apiKeyEnv"
                )
            if not config.get("apiKeyEnv"):
                raise ValueError("请输入 API Key")
            if not config.get("models"):
                config["models"] = [ModelConfig(id=config["model"]).model_dump()]
        item = await save_profile(
            session,
            profile_id=profile_id,
            name=body.name,
            kind=body.kind,
            config=config,
            is_default=body.isDefault,
            active=body.active,
        )
        if item.kind == "agent" and item.active and config.get("adapter") == "pi":
            await agent_snapshot(session, item.id)
        await session.commit()
        return view(item)
    except (ValueError, LookupError) as exc:
        await session.rollback()
        if new_reference:
            discard_runtime_key(new_reference)
        raise HTTPException(422, str(exc)) from exc
    except Exception:
        await session.rollback()
        if new_reference:
            discard_runtime_key(new_reference)
        raise


@router.post("", status_code=201)
async def create(body: ProfileInput, session: AsyncSession = Depends(get_session)):
    return await persist(body, session)


@router.put("/{profile_id}")
async def edit(profile_id: str, body: ProfileInput, session: AsyncSession = Depends(get_session)):
    return await persist(body, session, profile_id)


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agentProfileId: str | None = None
    reasoningEffort: Literal["low", "medium", "high"] | None = None
    traexModel: str | None = Field(default=None, min_length=1, max_length=200)
    modelProfileId: str | None = None
    model: str | None = Field(default=None, min_length=1, max_length=200)


@router.get("/conversations/{conversation_id}")
async def get_selection(conversation_id: str, session: AsyncSession = Depends(get_session)):
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None or conversation.deleted_at is not None:
        raise HTTPException(404, "对话不存在")
    return {
        "agentProfileId": conversation.agent_profile_id,
        "traexModel": conversation.traex_model,
        "reasoningEffort": conversation.reasoning_effort,
        "modelProfileId": conversation.model_profile_id,
        "model": conversation.agent_model,
    }


@router.put("/conversations/{conversation_id}")
async def select_agent(
    conversation_id: str,
    body: Selection,
    session: AsyncSession = Depends(get_session),
):
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None or conversation.deleted_at is not None:
        raise HTTPException(404, "对话不存在")
    try:
        await validate_traex_model(body.traexModel)
        await agent_snapshot(
            session,
            body.agentProfileId,
            body.traexModel,
            body.reasoningEffort,
            body.modelProfileId,
            body.model,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    conversation.agent_profile_id = body.agentProfileId
    conversation.traex_model = body.traexModel
    conversation.reasoning_effort = body.reasoningEffort
    conversation.model_profile_id = body.modelProfileId
    conversation.agent_model = body.model
    await session.commit()
    return {
        "agentProfileId": conversation.agent_profile_id,
        "traexModel": conversation.traex_model,
        "reasoningEffort": conversation.reasoning_effort,
        "modelProfileId": conversation.model_profile_id,
        "model": conversation.agent_model,
    }


@router.post("/{profile_id}/probe")
async def probe(profile_id: str, session: AsyncSession = Depends(get_session)):

    from app.application.agent_provider import probe_provider
    from app.application.runtime_config import LLMRuntimeConfig, agent_runtime_scope
    from app.application.runtime_profiles import restore_agent_snapshot
    from app.runtime.models import OpenAICompatibleAdapter

    item = await session.get(RuntimeProfile, profile_id)
    if item is None or not item.active:
        raise HTTPException(404, "配置不存在或已停用")
    schema = {
        "type": "object",
        "properties": {"reply": {"const": "ok"}},
        "required": ["reply"],
        "additionalProperties": False,
    }
    original_config = dict(item.config_json)
    result = {}
    try:
        if item.kind == "agent":
            runtime = restore_agent_snapshot(await agent_snapshot(session, profile_id))
            await session.rollback()
            with agent_runtime_scope(runtime):
                result = await probe_provider()
            result = {
                "runtimeDigest": runtime.digest,
                "status": "available" if result.get("ok") else "failed",
                "summary": result.get("summary"),
                "code": result.get("code"),
            }

        else:
            config = dict(item.config_json)
            await session.rollback()
            await OpenAICompatibleAdapter(
                LLMRuntimeConfig(
                    "openai_compatible",
                    config["baseUrl"],
                    config["model"],
                    config["apiKeyEnv"],
                    credential_configured=bool(resolve_runtime_key(config["apiKeyEnv"])),
                ),
                timeout_seconds=config.get("timeoutSeconds", 60),
                structured_mode=config.get("structuredMode", "json_schema"),
            ).generate('Return {"reply":"ok"}', schema, 128)
            result = {"status": "available", "summary": "真实模型输出契约验证成功"}
    except Exception as exc:
        result = {
            "status": "failed",
            "code": getattr(exc, "code", "PROBE_FAILED"),
            "summary": "探测失败，请检查配置、登录和网络",
        }

    current = await session.get(RuntimeProfile, profile_id, populate_existing=True)
    if current is not None and current.config_json == original_config:
        current.probe_json = result
        await session.commit()
    return result


@router.get("/traex/models")
async def traex_models():
    try:
        return await list_traex_models()
    except (ValueError, OSError) as exc:
        detail = str(exc) if isinstance(exc, ValueError) else "无法启动 TraeX 模型查询"
        raise HTTPException(503, detail) from exc
