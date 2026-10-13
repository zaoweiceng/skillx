import json
from uuid import uuid4

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from app.api.skills import (
    SkillConversion,
    SkillInput,
    convert,
    create_skill,
    delete_skill,
    update_skill,
)
from app.application.agent_host import AgentHost, AgentHostRequest
from app.application.callable_capabilities import callable_catalog, resolve_callable
from app.application.managed_skills import (
    DEFAULT_INPUT,
    DEFAULT_OUTPUT,
    convert_skill,
    copy_source,
    managed_skill_context,
    save_skill,
    skill_view,
    source_path,
)
from app.application.skillx_execution import execute_skillx
from app.config import get_settings
from app.infrastructure.database import SessionLocal
from app.main import app

SOURCE = (
    'def execute(request, context):\n    return {"answer": str(request.input["task"]).upper()}\n'
)
VALUES = dict(
    name="report-helper",
    displayName="报告助手",
    description="整理报告",
    content="隐藏原始指令 MARKER-RAW-SKILL：先理解任务，再输出报告。",
    enabled=True,
)
EXAMPLES = [
    {"input": {"task": "hello"}, "expected": {"answer": "HELLO"}},
    {"input": {"task": "world"}, "expected": {"answer": "WORLD"}},
]


@pytest.fixture(autouse=True)
def roots(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "artifact_root", tmp_path / "artifacts")
    monkeypatch.setattr(get_settings(), "capability_root", tmp_path / "capabilities")


def options(kind="direct", **extra):
    return dict(
        kind=kind,
        inputSchema=DEFAULT_INPUT,
        outputSchema=DEFAULT_OUTPUT,
        source=SOURCE,
        examples=EXAMPLES,
        **extra,
    )


async def test_direct_conversion_executes_and_edit_invalidates_discovery():
    async with SessionLocal() as session:
        skill = await save_skill(session, VALUES)
        await session.commit()
        assert "MARKER-RAW-SKILL" in json.dumps(await managed_skill_context(session))
        await convert_skill(session, skill, options())
        await session.commit()
        published_id = skill.skillx_version_id
        context = await managed_skill_context(session)
        assert context["skills"] == []
        assert context["skillx"][0]["id"] == published_id
        assert "MARKER-RAW-SKILL" not in json.dumps(context)
        assert (await callable_catalog(session))[0]["id"] == published_id
        view = await skill_view(session, skill)
        assert view["mode"] == "skillx" and view["interfaceKind"] == "direct"
        detail = await skill_view(session, skill, detail=True)
        assert detail["conversion"]["kind"] == "direct"
        assert detail["conversion"]["source"] == SOURCE
        assert detail["conversion"]["examples"] == EXAMPLES
    result = await execute_skillx(published_id, "execute", {"task": "real call"}, str(uuid4()))
    assert result.status == "succeeded" and result.output == {"answer": "REAL CALL"}
    async with SessionLocal() as session:
        from app.infrastructure.models import ManagedSkill

        skill = await session.get(ManagedSkill, skill.id)
        await save_skill(session, {**VALUES, "content": VALUES["content"] + "\n新指令"}, skill)
        await session.commit()
        assert skill.skillx_version_id is None
        assert await callable_catalog(session) == []
        assert "MARKER-RAW-SKILL" in json.dumps(await managed_skill_context(session))
        # Existing pinned versions keep working after edits and deletion.
        assert (await resolve_callable(session, published_id)).contract["id"] == published_id
        await convert_skill(session, skill, options())
        await session.commit()
        assert (await skill_view(session, skill))["skillxVersion"] == "1.0.1"
        await save_skill(session, {**VALUES, "content": skill.content, "enabled": False}, skill)
        await session.commit()
        assert await callable_catalog(session) == []
        assert await managed_skill_context(session) == {"skills": [], "skillx": []}
        await delete_skill(skill.id, session)
    result = await execute_skillx(published_id, "execute", {"task": "retained"}, str(uuid4()))
    assert result.output == {"answer": "RETAINED"}


async def test_failed_conversion_never_switches_to_skillx():
    async with SessionLocal() as session:
        skill = await save_skill(session, VALUES)
        await session.commit()
        with pytest.raises(ValueError, match="验证未通过"):
            await convert_skill(
                session,
                skill,
                {
                    **options(),
                    "source": 'def execute(request, context):\n    return {"answer": "wrong"}\n',
                },
            )
        assert skill.skillx_version_id is None
        assert await callable_catalog(session) == []
        with pytest.raises(ValueError, match="不同输入"):
            await convert_skill(session, skill, {**options(), "examples": EXAMPLES[:1] * 2})
        assert "MARKER-RAW-SKILL" in json.dumps(await managed_skill_context(session))


async def test_import_resources_and_agent_prompt_are_used_only_when_invoked(tmp_path, monkeypatch):
    imported = tmp_path / "source"
    imported.mkdir()
    (imported / "SKILL.md").write_text(
        "---\nname: report-helper\ndescription: 整理报告\n---\n\nMARKER-RAW-SKILL"
    )
    (imported / "references").mkdir()
    (imported / "references" / "report.md").write_text("MARKER-REFERENCE 参考格式")
    async with SessionLocal() as session:
        skill = await save_skill(session, VALUES, imported=imported)
        await convert_skill(session, skill, options("agent"))
        await session.commit()
        version_id = skill.skillx_version_id
        assert "MARKER-REFERENCE" not in json.dumps(await managed_skill_context(session))
        assert "MARKER-RAW-SKILL" not in json.dumps(await callable_catalog(session))
        assert (
            "references/report.md" in (await skill_view(session, skill, detail=True))["resources"]
        )
    requests = []

    async def execute(self, request, **kwargs):
        from app.application.agent_host import AgentHostResult

        requests.append(request)
        return AgentHostResult(output={"answer": "完成"})

    monkeypatch.setattr(AgentHost, "execute", execute)
    result = await execute_skillx(version_id, "execute", {"task": "整理"}, str(uuid4()))
    assert result.output == {"answer": "完成"}
    assert "MARKER-RAW-SKILL" in requests[0].prompt
    assert "MARKER-REFERENCE" in requests[0].prompt
    assert requests[0].requested_capabilities == ()
    assert requests[0].include_managed_skills is False


async def test_host_receives_only_contract_after_conversion(tmp_path):
    class CaptureAdapter:
        def __init__(self):
            self.prompts = []

        async def events(self, run_id, request):
            self.prompts.append(request.prompt)
            raise RuntimeError("captured")
            yield

    adapter = CaptureAdapter()
    host = AgentHost(adapter=adapter)
    async with SessionLocal() as session:
        skill = await save_skill(session, VALUES)
        await session.commit()
    request = AgentHostRequest(
        run_id=str(uuid4()),
        prompt="任务",
        output_schema=DEFAULT_OUTPUT,
        working_directory=tmp_path,
    )
    with pytest.raises(RuntimeError, match="captured"):
        await host.execute(request)
    assert "MARKER-RAW-SKILL" in adapter.prompts[-1]
    async with SessionLocal() as session:
        from app.infrastructure.models import ManagedSkill

        skill = await session.get(ManagedSkill, skill.id)
        await convert_skill(session, skill, options("agent"))
        await session.commit()
    with pytest.raises(RuntimeError, match="captured"):
        await host.execute(request)
    assert "MARKER-RAW-SKILL" not in adapter.prompts[-1]
    assert "inputSchema" in adapter.prompts[-1]
    assert "优先按契约调用" in adapter.prompts[-1]


async def test_revision_conflict_and_snapshot_drift_fail_closed():
    async with SessionLocal() as session:
        result = await create_skill(SkillInput(**VALUES), session)
        updated = await update_skill(
            result["id"], SkillInput(**{**VALUES, "content": "新版指令"}), session
        )
        with pytest.raises(HTTPException) as conflict:
            await convert(
                result["id"],
                SkillConversion(**options("agent"), expectedRevision=result["revision"]),
                session,
            )
        assert conflict.value.status_code == 409
        from app.infrastructure.models import ManagedSkill

        skill = await session.get(ManagedSkill, result["id"])
        (source_path(skill) / "SKILL.md").write_text("changed outside API")
        with pytest.raises(ValueError, match="快照已发生变化"):
            await convert_skill(session, skill, options("agent"))
        assert updated["mode"] == "skill"


def test_import_rejects_symlink_and_does_not_copy(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("guide")
    (source / "external").symlink_to(tmp_path, target_is_directory=True)
    target = tmp_path / "copy"
    with pytest.raises(ValueError, match="符号链接"):
        copy_source(source, target)
    assert not target.exists()


async def test_http_crud_and_schema_errors():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/skills")).json() == []
        preview = await client.post(
            "/api/skills/preview",
            json={
                "content": (
                    "---\nname: sample\ndisplayName: 示例\ndescription: |\n  多行用途\n---\n正文"
                )
            },
        )
        assert preview.status_code == 200
        assert preview.json() == {
            "name": "sample",
            "displayName": "示例",
            "description": "多行用途\n",
        }
        assert (await client.get("/api/skills")).json() == []
        created = await client.post("/api/skills", json=VALUES)
        assert created.status_code == 201, created.text
        id = created.json()["id"]
        assert (await client.get(f"/api/skills/{id}")).json()["content"].startswith("---")
        bad = await client.post(
            f"/api/skills/{id}/convert",
            json={**options("agent"), "expectedRevision": 1, "inputSchema": {"type": "string"}},
        )
        assert bad.status_code == 422
        assert (await client.get(f"/api/skills/{id}")).json()["mode"] == "skill"
        assert (await client.delete(f"/api/skills/{id}")).status_code == 204
        assert (await client.get("/api/skills")).json() == []
        assert (await client.get(f"/api/skills/{id}")).status_code == 404


async def test_skill_discovery_only_loads_instructions_on_demand():
    from pathlib import Path

    async with SessionLocal() as session:
        await save_skill(session, VALUES)
        await session.commit()
        context = await managed_skill_context(session, include_instructions=False)
        entry = context['skills'][0]
        assert entry['description'] == VALUES['description']
        assert 'MARKER-RAW-SKILL' not in json.dumps(context)
        assert 'MARKER-RAW-SKILL' in Path(entry['instructionsPath']).read_text()
