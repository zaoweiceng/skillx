"""Validate stable model SkillX prompts in a fresh package and real Host runtime."""

import json
import shutil
from types import SimpleNamespace
from uuid import uuid4

import yaml
from jsonschema import Draft202012Validator
from sqlalchemy import select

from app.application.capabilities import snapshot_manifest
from app.application.compiler import digest_json
from app.application.runtime_profiles import agent_snapshot, workflow_profiles
from app.application.skillx_execution import execute_skillx
from app.config import get_settings
from app.domain.workflow import WorkflowNode
from app.infrastructure.database import Capability, CapabilityVersion, SessionLocal
from app.runtime.skillx import load_manifest, package_digest


async def model_runtime(session, kind):
    node = WorkflowNode(id="candidate", type=kind, displayName="候选能力")
    profiles = await workflow_profiles(session, SimpleNamespace(spec=SimpleNamespace(nodes=[node])))
    return {
        "agentConfig": await agent_snapshot(session, None),
        "modelSnapshot": profiles.get("candidate"),
    }


async def materialize_model_skill(proposal, job_id, evidence):
    if not proposal.prompt.strip() or proposal.source or proposal.dependencies:
        raise ValueError("模型 SkillX 必须提供独立 prompt，不能混入未验证代码")
    from app.application.learning_evidence import validate_examples
    validate_examples(proposal.examples, evidence)
    settings = get_settings()
    root = settings.capability_root.resolve() / "generated" / job_id / proposal.name
    root.mkdir(parents=True, exist_ok=True)
    async with SessionLocal() as session:
        previous = await session.scalar(
            select(CapabilityVersion)
            .join(Capability)
            .where(Capability.name == proposal.name)
            .order_by(CapabilityVersion.created_at.desc())
            .limit(1)
        )
        number = "0.1.0"
        if previous:
            major, minor, patch = previous.version.split(".")
            number = f"{major}.{minor}.{int(patch) + 1}"
        runtime = await model_runtime(session, proposal.kind)
    selected_agent = (runtime.get("modelSnapshot", {}).get("agentConfig", {})
                      if proposal.kind == "llm" else runtime["agentConfig"])
    if selected_agent.get("type") in {"fixture", "unavailable"} or selected_agent.get("provider") == "fixture":
        raise ValueError("模型能力发布必须使用真实模型，fixture 不能作为发布证据")
    (root / "SKILL.md").write_text(
        "---\n"
        + yaml.safe_dump(
            {"name": proposal.name, "description": proposal.description}, allow_unicode=True
        )
        + "---\n"
        + proposal.description
    )
    (root / "prompt.md").write_text(proposal.prompt)
    (root / "input.json").write_text(json.dumps(proposal.inputSchema))
    (root / "output.json").write_text(json.dumps(proposal.outputSchema))
    (root / "requirements.lock").write_text("# Model interface; no local package dependencies.\n")
    manifest = {
        "apiVersion": "skillx.dev/v1alpha1",
        "kind": "SkillX",
        "metadata": {"name": proposal.name, "description": proposal.description, "version": number},
        "spec": {
            "guide": {"entry": "SKILL.md"},
            "defaultInterface": "execute",
            "interfaces": {
                "execute": {
                    "kind": proposal.kind,
                    "description": proposal.description,
                    "inputSchema": "input.json",
                    "outputSchema": "output.json",
                    "prompt": {"entry": "prompt.md"},
                    "effects": [],
                    "capabilities": [],
                    "execution": {"timeoutSeconds": 120, "idempotency": "inherent"},
                },
            },
        },
    }
    (root / "skillx.yaml").write_text(yaml.safe_dump(manifest, allow_unicode=True))
    load_manifest(root)
    clean = (
        settings.capability_root.resolve() / "generated" / job_id / ("validation-" + str(uuid4()))
    )
    shutil.copytree(root, clean)
    version = SimpleNamespace(
        id="candidate-" + str(uuid4()),
        status="published",
        digest=package_digest(clean),
        package_path=str(clean.relative_to(settings.capability_root.resolve())),
        manifest_json=snapshot_manifest(clean, manifest),
    )
    proofs = []
    for index, example in enumerate(proposal.examples):
        Draft202012Validator(proposal.inputSchema).validate(example.input)
        result = await execute_skillx(
            version.id,
            "execute",
            example.input,
            f"model-validation-{job_id}-{index}-{uuid4()}",
            runtime,
            _candidate_version=version,
        )
        if result.status != "succeeded" or result.output != example.expected:
            raise ValueError(
                f"模型能力真实业务断言 {index} 未通过: {result.error or result.output}"
            )
        proofs.append(
            {
                "inputDigest": digest_json(example.input),
                "outputDigest": digest_json(result.output),
                "usage": result.metrics,
            }
        )
    proof = {
        "profile": "model",
        "kind": proposal.kind,
        "runtime": runtime,
        "status": "passed",
        "examples": proofs,
        "promptDigest": digest_json(proposal.prompt),
    }
    (root / "validation.json").write_text(json.dumps(proof))
    (root / "tests.json").write_text(json.dumps([x.portable_dump() for x in proposal.examples]))
    return root
