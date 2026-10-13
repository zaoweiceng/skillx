from pathlib import Path
from typing import Literal

import yaml
from fastapi import APIRouter, Depends, HTTPException
from jsonschema.exceptions import SchemaError, ValidationError
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.managed_skills import (
    convert_skill,
    import_target,
    parse_skill,
    save_skill,
    skill_view,
)
from app.infrastructure.database import get_session
from app.infrastructure.models import ManagedSkill
from app.infrastructure.models.base import utcnow

router = APIRouter(tags=["Skills"])


class SkillInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    displayName: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=2000)
    content: str = Field(min_length=1, max_length=131072)
    enabled: bool = True


class SkillImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=4096)


class SkillContent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=131072)


class ConversionExample(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input: dict
    expected: dict


class SkillConversion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["direct", "agent", "existing"]
    expectedRevision: int = Field(ge=1)
    inputSchema: dict = Field(default_factory=dict)
    outputSchema: dict = Field(default_factory=dict)
    source: str = Field(default="", max_length=131072)
    examples: list[ConversionExample] = Field(default_factory=list, max_length=20)


async def require_skill(session, id: str) -> ManagedSkill:
    skill = await session.get(ManagedSkill, id)
    if not skill or skill.deleted_at:
        raise HTTPException(404, "Skill 不存在")
    return skill


@router.get("/skills")
async def list_skills(session: AsyncSession = Depends(get_session)):
    skills = await session.scalars(
        select(ManagedSkill)
        .where(ManagedSkill.deleted_at.is_(None))
        .order_by(ManagedSkill.created_at.desc())
    )
    return [await skill_view(session, skill) for skill in skills]


@router.post("/skills", status_code=201)
async def create_skill(body: SkillInput, session: AsyncSession = Depends(get_session)):
    try:
        skill = await save_skill(session, body.model_dump())
        result = await skill_view(session, skill, detail=True)
        await session.commit()
        return result
    except (ValueError, OSError) as exc:
        await session.rollback()
        raise HTTPException(422, str(exc)) from exc


@router.post("/skills/preview")
async def preview_skill(body: SkillContent):
    try:
        metadata, _ = parse_skill(body.content)
        return {
            key: metadata.get(key) if isinstance(metadata.get(key), str) else ""
            for key in ("name", "displayName", "description")
        }
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/skills/import", status_code=201)
async def import_skill(body: SkillImport, session: AsyncSession = Depends(get_session)):
    try:
        path = Path(body.path).expanduser().resolve()
        file = path / "SKILL.md"
        if not file.is_file() or file.stat().st_size > 128 * 1024:
            raise ValueError("目录中缺少有效的 SKILL.md，或文件超过 128 KB")
        content = file.read_text(encoding="utf-8")
        metadata, _ = parse_skill(content)
        values = SkillInput(
            name=metadata.get("name", ""),
            displayName=metadata.get("displayName") or metadata.get("name", ""),
            description=metadata.get("description", ""),
            content=content,
        )
        existing = await import_target(session, values.name, path)
        imported_values = values.model_dump()
        if existing:
            imported_values["enabled"] = existing.enabled
        skill = await save_skill(session, imported_values, existing, imported=path)
        result = await skill_view(session, skill, detail=True)
        await session.commit()
        return result
    except (ValueError, OSError) as exc:
        await session.rollback()
        raise HTTPException(422, str(exc)) from exc


@router.get("/skills/{id}")
async def get_skill(id: str, session: AsyncSession = Depends(get_session)):
    return await skill_view(session, await require_skill(session, id), detail=True)


@router.put("/skills/{id}")
async def update_skill(id: str, body: SkillInput, session: AsyncSession = Depends(get_session)):
    skill = await require_skill(session, id)
    try:
        await save_skill(session, body.model_dump(), skill)
        result = await skill_view(session, skill, detail=True)
        await session.commit()
        return result
    except (ValueError, OSError) as exc:
        await session.rollback()
        raise HTTPException(422, str(exc)) from exc


@router.delete("/skills/{id}", status_code=204)
async def delete_skill(id: str, session: AsyncSession = Depends(get_session)):
    skill = await require_skill(session, id)
    # Keep immutable versions available for existing workflow bindings.
    skill.deleted_at = utcnow()
    skill.enabled = False
    await session.commit()


@router.post("/skills/{id}/convert")
async def convert(id: str, body: SkillConversion, session: AsyncSession = Depends(get_session)):
    skill = await require_skill(session, id)
    if body.expectedRevision != skill.revision:
        raise HTTPException(409, "Skill 已更新，请关闭转换窗口后重新打开")
    try:
        await convert_skill(session, skill, body.model_dump())
        result = await skill_view(session, skill, detail=True)
        await session.commit()
        return result
    except (
        ValueError,
        OSError,
        SyntaxError,
        SchemaError,
        ValidationError,
        TimeoutError,
        yaml.YAMLError,
    ) as exc:
        await session.rollback()
        raise HTTPException(422, str(exc)) from exc
