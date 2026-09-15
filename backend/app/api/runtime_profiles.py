"""Configuration endpoints independent from workflow business routes."""

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.runtime_profiles import agent_snapshot, list_profiles, save_profile
from app.infrastructure.database import Conversation, RuntimeProfile, get_session

router = APIRouter(prefix="/runtime-profiles", tags=["Runtime profiles"])


class ProfileConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    adapter: Literal["traex", "pi"] | None = None
    modelProfileId: str | None = None
    baseUrl: str | None = Field(default=None, pattern=r"^https?://[^\s]+$")
    model: str | None = None
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
            if not all((self.config.baseUrl, self.config.model, self.config.apiKeyEnv)):
                raise ValueError("模型配置需要 Base URL、模型名称和密钥环境变量引用")
        elif not self.config.adapter or (
            self.config.adapter == "pi" and not self.config.modelProfileId
        ):
            raise ValueError("Agent 需要执行体；Pi 还需要模型配置")
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
    }


@router.get("")
async def listing(session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    return [view(item) for item in await list_profiles(session)]


async def persist(body: ProfileInput, session: AsyncSession, profile_id: str | None = None):
    try:
        item = await save_profile(
            session,
            profile_id=profile_id,
            name=body.name,
            kind=body.kind,
            config=body.config.model_dump(exclude_none=True),
            is_default=body.isDefault,
            active=body.active,
        )
        await session.commit()
        return view(item)
    except (ValueError, LookupError) as exc:
        await session.rollback()
        raise HTTPException(422, str(exc)) from exc


@router.post("", status_code=201)
async def create(body: ProfileInput, session: AsyncSession = Depends(get_session)):
    return await persist(body, session)


@router.put("/{profile_id}")
async def edit(profile_id: str, body: ProfileInput, session: AsyncSession = Depends(get_session)):
    return await persist(body, session, profile_id)


class Selection(BaseModel):
    agentProfileId: str | None = None


@router.get("/conversations/{conversation_id}")
async def get_selection(conversation_id: str, session: AsyncSession = Depends(get_session)):
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(404, "对话不存在")
    return {"agentProfileId": conversation.agent_profile_id}


@router.put("/conversations/{conversation_id}")
async def select_agent(
    conversation_id: str,
    body: Selection,
    session: AsyncSession = Depends(get_session),
):
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(404, "对话不存在")
    try:
        await agent_snapshot(session, body.agentProfileId)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    conversation.agent_profile_id = body.agentProfileId
    await session.commit()
    return {"agentProfileId": conversation.agent_profile_id}


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
                    "openai_compatible", config["baseUrl"], config["model"], config["apiKeyEnv"]
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
