"""Durable invocations shared by tasks and manual capability execution."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from uuid import uuid4

from jsonschema import Draft202012Validator
from sqlalchemy import func, select, update
from sqlalchemy.orm import aliased

from app.application.compiler import digest_json
from app.application.local_execution import (
    execute_local,
    task_directory,
    validate_working_directory,
)
from app.application.runtime_profiles import agent_snapshot
from app.application.security import redact
from app.application.task_budget import now, record_activity
from app.application.tasks import TERMINAL, emit
from app.config import get_settings
from app.infrastructure.database import (
    CapabilityVersion,
    ModelUsage,
    NodeRun,
    Run,
    SessionLocal,
    WorkflowVersion,
    utcnow,
)
from app.infrastructure.models.tasks import HumanInteraction, Invocation, Task


async def enqueue_invocation(
    session, *, kind: str, arguments: dict, key: str, task: Task | None = None
):
    # Resolve before idempotency checks so legacy and unified entrypoints share
    # the same identity, authorization snapshot, and unknown-outcome guard.
    if kind == "capability":
        from app.application.callable_capabilities import resolve_callable

        if set(arguments) - {"capabilityVersionId", "interface", "input"}:
            raise ValueError("能力调用包含未知字段")
        version_id = arguments.get("capabilityVersionId")
        if not isinstance(version_id, str) or not version_id:
            raise ValueError("能力调用需要 capabilityVersionId")
        target = await resolve_callable(session, version_id)
        kind, arguments = target.bind(
            arguments.get("interface", "execute"), arguments.get("input", {})
        )
    scope = task.id if task else "manual"
    existing = await session.scalar(
        select(Invocation).where(Invocation.scope == scope, Invocation.call_key == key)
    )
    if existing:
        if existing.kind != kind or digest_json(existing.request_json) != digest_json(arguments):
            raise ValueError("幂等键对应的执行请求已变化")
        return existing
    if task:
        for previous in await session.scalars(
            select(Invocation).where(Invocation.task_id == task.id, Invocation.kind == kind)
        ):
            code = (previous.error_json or {}).get("code")
            if code in {"UNKNOWN_OUTCOME", "PERMISSION_DENIED"} and (
                digest_json(previous.request_json) == digest_json(arguments)
            ):
                refused = Invocation(
                    id=str(uuid4()),
                    task_id=task.id,
                    scope=scope,
                    call_key=key,
                    kind=kind,
                    status="failed",
                    request_json=arguments,
                    error_json={"code": code, "message": "相同操作已拒绝或结果未知，禁止自动重放"},
                )
                session.add(refused)
                await session.flush()
                emit(
                    session,
                    task.id,
                    "invocation.failed",
                    {"id": refused.id, "error": refused.error_json},
                )
                return refused
    needs_approval = False
    if kind == "skillx":
        version = await session.get(CapabilityVersion, arguments.get("versionId"))
        if version is None or version.status != "published":
            raise ValueError("SkillX 发布版本不存在")
        interface = version.manifest_json["interfaces"].get(arguments.get("interface"))
        if interface is None:
            raise ValueError("SkillX 接口不存在")
        Draft202012Validator(interface["inputSchema"]).validate(arguments.get("input", {}))
        needs_approval = any(
            e.get("confirmation") == "required" for e in interface.get("effects", [])
        )
    elif kind == "workflow":
        version = await session.get(WorkflowVersion, arguments.get("versionId"))
        if version is None:
            raise ValueError("工作流版本不存在")
        Draft202012Validator(version.definition_json["spec"]["inputSchema"]).validate(
            arguments.get("input", {})
        )
    elif kind == "model":
        if task is None:
            raise ValueError("模型探索需要 Task")
        if set(arguments) - {"prompt", "input", "outputSchema"}:
            raise ValueError("模型调用包含未知字段")
        if not isinstance(arguments.get("prompt"), str) or not arguments["prompt"].strip():
            raise ValueError("模型调用需要独立任务 prompt")
        if not isinstance(arguments.get("input"), dict):
            raise ValueError("模型调用 input 必须是对象")
        schema = arguments.get("outputSchema", {})
        Draft202012Validator.check_schema(schema)
        if schema.get("type") != "object":
            raise ValueError("模型输出必须是 object Schema")
    elif kind == "tool":
        if arguments.get("capabilityRef") not in get_settings().agent_host_capabilities:
            raise PermissionError("工具未获得 Host 授权")
    elif kind in {"code", "install"}:
        if task is None or not get_settings().task_local_execution_enabled:
            raise PermissionError("本机执行需要已授权 Task")
        if kind == "install":
            if arguments.get("scope", "task") not in {"task", "system"}:
                raise ValueError("安装 scope 必须是 task 或 system")
            needs_approval = arguments.get("scope") == "system"
    else:
        raise ValueError("不支持的调用类型")
    if task is None:
        task = Task(
            id=str(uuid4()),
            goal="手动能力执行",
            status="manual",
            state_json={},
            runtime_json=await agent_snapshot(session, None),
        )
        session.add(task)
        await session.flush()
    invocation = Invocation(
        id=str(uuid4()),
        task_id=task.id if task else None,
        scope=scope,
        call_key=key,
        kind=kind,
        request_json=arguments,
        runtime_json=task.runtime_json if task else await agent_snapshot(session, None),
    )
    if kind == "skillx":
        from types import SimpleNamespace

        from app.application.runtime_profiles import workflow_profiles
        from app.domain.workflow import WorkflowNode

        node = WorkflowNode(
            id="invoke",
            type="skillx",
            displayName="SkillX",
            config={
                "capabilityVersionId": arguments["versionId"],
                "interface": arguments["interface"],
            },
        )
        snapshots = await workflow_profiles(
            session, SimpleNamespace(spec=SimpleNamespace(nodes=[node]))
        )
        invocation.runtime_json = {
            "agentConfig": invocation.runtime_json,
            "modelSnapshot": snapshots.get("invoke"),
        }
    if kind in {"code", "install"}:
        invocation.runtime_json = {**invocation.runtime_json, "localSupervisorVersion": 1}
    session.add(invocation)
    await session.flush()
    if task:
        emit(
            session,
            task.id,
            "invocation.queued",
            {
                "id": invocation.id,
                "kind": kind,
                "request": arguments,
                "reason": next(
                    (
                        c.get("reason")
                        for c in task.state_json.get("calls", [])
                        if c.get("key") == key
                    ),
                    None,
                ),
            },
        )
    if needs_approval:
        if task is None:
            task = Task(
                id=str(uuid4()),
                goal="手动 SkillX 执行",
                status="manual",
                state_json={},
                runtime_json=invocation.runtime_json,
            )
            session.add(task)
            await session.flush()
            invocation.task_id = task.id
        payload = {
            "title": "批准本次系统工具安装" if kind == "install" else "批准本次 SkillX 操作",
            "reason": (
                "此操作超出任务目录；请核对包、版本、安装器和提权要求。"
                if kind == "install"
                else "能力声明要求人工确认，请核对本次输入。"
            ),
            "invocationId": invocation.id,
            "operation": arguments,
            "inputSchema": {
                "type": "object",
                "properties": {"approved": {"type": "boolean"}},
                "required": ["approved"],
                "additionalProperties": False,
            },
        }
        session.add(
            HumanInteraction(
                id=str(uuid4()),
                task_id=task.id,
                call_key="approve:" + invocation.id,
                kind="authorization",
                request_json=payload,
                snapshot_digest=digest_json(payload),
            )
        )
        invocation.status = "waiting_user"
    return invocation


class InvocationDriver:
    async def poll_once(self):
        async with SessionLocal() as session, session.begin():
            from app.application.human_projection import sync_workflow_interactions

            await sync_workflow_interactions(session)
            for waiting in await session.scalars(
                select(Invocation).where(Invocation.status == "waiting_user")
            ):
                approval = await session.scalar(
                    select(HumanInteraction).where(
                        HumanInteraction.call_key == "approve:" + waiting.id
                    )
                )
                if approval and approval.status == "answered":
                    if approval.request_json.get("operation") != waiting.request_json:
                        waiting.status = "failed"
                        waiting.error_json = {"code": "STALE_APPROVAL", "message": "授权操作已变化"}
                    elif approval.answer_json.get("approved") is True:
                        waiting.status = "queued"
                    else:
                        waiting.status = "failed"
                        waiting.error_json = {
                            "code": "PERMISSION_DENIED",
                            "message": "用户拒绝本次操作",
                        }
            # Workflow waits never monopolize the queue.
            for waiting in await session.scalars(
                select(Invocation).where(Invocation.status == "waiting_execution")
            ):
                run = await session.get(Run, waiting.run_id)
                if run and run.status in TERMINAL:
                    waiting.status, waiting.output_json, waiting.error_json = (
                        run.status,
                        run.output_json or {},
                        run.error_json,
                    )
                    if waiting.task_id:
                        node_runs = await session.scalars(
                            select(NodeRun).where(
                                NodeRun.run_id == run.id, NodeRun.node_type != "approval"
                            )
                        )
                        await record_activity(
                            session,
                            waiting.task_id,
                            "run:" + run.id,
                            [
                                (n.started_at, n.finished_at)
                                for n in node_runs
                                if n.finished_at and n.started_at
                            ],
                        )
                        tokens = await session.scalar(
                            select(func.sum(ModelUsage.total_tokens)).where(
                                ModelUsage.run_id == run.id
                            )
                        )
                        await session.execute(
                            update(Task)
                            .where(Task.id == waiting.task_id)
                            .values(
                                total_tokens=Task.total_tokens + (tokens or 0),
                            )
                        )
                        emit(
                            session,
                            waiting.task_id,
                            "invocation." + waiting.status,
                            {
                                "id": waiting.id,
                                "output": waiting.output_json,
                                "error": waiting.error_json,
                            },
                        )
            # Unknown outcomes are not automatically replayed after a lease expires.
            for expired in await session.scalars(
                select(Invocation).where(
                    Invocation.status == "running",
                    Invocation.lease_expires_at < utcnow(),
                )
            ):
                if expired.kind in {"code", "install"} and expired.runtime_json.get(
                    "localSupervisorVersion"
                ):
                    expired.status = "queued"
                    expired.lease_token, expired.lease_expires_at = None, None
                    if expired.task_id:
                        emit(session, expired.task_id, "invocation.reattached", {"id": expired.id})
                    continue
                completed = None
                if expired.kind in {"code", "install"}:
                    path = (
                        task_directory(expired.scope)
                        / ".executions"
                        / expired.id
                        / "completed.json"
                    )
                    if path.is_file():
                        completed = json.loads(path.read_text())
                expired.status = "succeeded" if completed else "failed"
                expired.output_json = completed or {}
                expired.error_json = (
                    None
                    if completed
                    else {"code": "UNKNOWN_OUTCOME", "message": "执行租约丢失，禁止自动重放"}
                )
                expired.lease_token = None
            other = aliased(Invocation)
            item = await session.scalar(
                select(Invocation)
                .where(
                    Invocation.status == "queued",
                    ~select(other.id)
                    .where(
                        other.scope == Invocation.scope,
                        other.status == "running",
                        other.kind.in_(["code", "install"]),
                        Invocation.kind.in_(["code", "install"]),
                    )
                    .exists(),
                )
                .order_by(Invocation.created_at)
                .limit(1)
            )
            if item is None:
                return False
            if item.kind == "workflow":
                claimed = await session.execute(
                    update(Invocation)
                    .where(
                        Invocation.id == item.id,
                        Invocation.status == "queued",
                    )
                    .values(status="dispatching")
                )
                if claimed.rowcount != 1:
                    return False
                from app.application.orchestrator import Orchestrator

                version = await session.get(WorkflowVersion, item.request_json["versionId"])
                try:
                    async with session.begin_nested():
                        run = await Orchestrator().create_run(
                            session,
                            version,
                            item.request_json.get("input", {}),
                            automatic_recovery=True,
                        )
                        owner_task = await session.get(Task, item.task_id) if item.task_id else None
                        if owner_task and owner_task.state_json.get("workingDirectory"):
                            run.runtime_snapshot_json = {**run.runtime_snapshot_json,
                                "workingDirectory": owner_task.state_json["workingDirectory"]}
                except Exception as exc:
                    item.status = "failed"
                    item.error_json = {
                        "code": getattr(exc, "code", "CAPABILITY_START_FAILED"),
                        "message": str(exc),
                    }
                    if item.task_id:
                        emit(
                            session,
                            item.task_id,
                            "invocation.failed",
                            {
                                "id": item.id,
                                "error": item.error_json,
                            },
                        )
                    return True
                item.run_id, item.status = run.id, "waiting_execution"
                return True
            token = str(uuid4())
            changed = await session.execute(
                update(Invocation)
                .where(Invocation.id == item.id, Invocation.status == "queued")
                .values(
                    status="running",
                    lease_token=token,
                    lease_expires_at=utcnow() + timedelta(seconds=360),
                )
            )
            if changed.rowcount != 1:
                return False
            if item.kind in {"code", "install"} and "activeStartedAt" not in item.runtime_json:
                item.runtime_json = {**item.runtime_json, "activeStartedAt": now().isoformat()}
            invocation_id, kind, args, scope = item.id, item.kind, item.request_json, item.scope
            task_context = await session.get(Task, item.task_id) if item.task_id else None
            working_directory = (
                (task_context.state_json or {}).get("workingDirectory") if task_context else None
            )
            runtime_json = item.runtime_json
            approval = await session.scalar(
                select(HumanInteraction).where(HumanInteraction.call_key == "approve:" + item.id)
            )
            effect_approved = bool(
                approval
                and approval.status == "answered"
                and approval.answer_json.get("approved") is True
                and approval.request_json.get("operation") == item.request_json
            )

        started = (
            datetime.fromisoformat(runtime_json["activeStartedAt"])
            if runtime_json.get("activeStartedAt")
            else now()
        )
        tokens = 0

        async def cancelled():
            async with SessionLocal() as session:
                current = await session.get(Invocation, invocation_id)
                return (
                    current is None or current.status != "running" or current.lease_token != token
                )

        try:
            async with asyncio.timeout(300):
                if kind in {"code", "install"}:
                    output = await execute_local(
                        scope,
                        invocation_id,
                        kind,
                        args,
                        cancelled,
                        allow_system=effect_approved,
                        **({"working_directory": working_directory} if working_directory else {}),
                    )
                elif kind == "model":
                    from app.application.agent_host import AgentHost, AgentHostRequest
                    from app.application.runtime_profiles import restore_agent_snapshot

                    async def audit_sink(runtime_event, audit_event):
                        async with SessionLocal() as audit_session, audit_session.begin():
                            current = await audit_session.get(Invocation, invocation_id)
                            if (
                                current
                                and current.lease_token == token
                                and current.status == "running"
                            ):
                                emit(
                                    audit_session,
                                    current.task_id,
                                    "agent.audit",
                                    {
                                        "invocationId": invocation_id,
                                        **audit_event,
                                    },
                                )

                    result = await AgentHost(runtime=restore_agent_snapshot(runtime_json)).execute(
                        AgentHostRequest(
                            run_id="model-" + invocation_id,
                            prompt=args["prompt"]
                            + "\n输入数据：\n"
                            + json.dumps(args["input"], ensure_ascii=False),
                            output_schema=args["outputSchema"],
                            working_directory=task_directory(scope) / "model" / invocation_id,
                            requested_capabilities=(),
                            max_steps=1,
                            max_duration_seconds=120,
                            max_total_tokens=get_settings().agent_max_total_tokens,
                        ),
                        event_sink=audit_sink,
                        cancellation=cancelled,
                    )
                    output, tokens = result.output, result.total_tokens
                    Draft202012Validator(args["outputSchema"]).validate(output)
                elif kind == "tool":
                    from app.application.tool_broker import (
                        ToolAdapterRegistry,
                        ToolExecutionContext,
                    )

                    output = await ToolAdapterRegistry().execute(
                        args["capabilityRef"],
                        ToolExecutionContext(
                            scope,
                            invocation_id,
                            validate_working_directory(working_directory)
                            if working_directory
                            else task_directory(scope),
                        ),
                        args.get("input", {}),
                    )
                else:
                    from app.application.skillx_execution import execute_skillx

                    result = await execute_skillx(
                        args["versionId"],
                        args["interface"],
                        args.get("input", {}),
                        invocation_id,
                        {**runtime_json, "workingDirectory": working_directory},
                        cancelled,
                        effect_approved=effect_approved,
                    )
                    tokens = int(result.metrics.get("totalTokens", 0))
                    if result.status != "succeeded":
                        async with SessionLocal() as session, session.begin():
                            current = await session.get(Invocation, invocation_id)
                            if current.lease_token == token and current.status == "running":
                                current.status = result.status
                                current.error_json = (
                                    result.error.model_dump() if result.error else None
                                )
                                if current.task_id:
                                    await record_activity(
                                        session,
                                        current.task_id,
                                        "invocation:" + current.id,
                                        [(started, now())],
                                    )
                                    await session.execute(
                                        update(Task)
                                        .where(Task.id == current.task_id)
                                        .values(total_tokens=Task.total_tokens + tokens)
                                    )
                                    emit(
                                        session,
                                        current.task_id,
                                        "invocation." + result.status,
                                        {"id": current.id, "error": current.error_json},
                                    )
                        return True
                    output = result.output or {}
            error = None
            status = "succeeded"
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
            return True
        except Exception as exc:
            if kind in {"code", "install"} and isinstance(exc, TimeoutError):
                from app.application.local_execution import cancel_local

                cancel_local(scope, invocation_id)
            output, status = {}, "failed"
            error = {"code": getattr(exc, "code", "EXECUTION_FAILED"), "message": str(exc)}
        async with SessionLocal() as session, session.begin():
            current = await session.get(Invocation, invocation_id)
            if current.status == "running" and current.lease_token == token:
                if current.task_id:
                    await record_activity(
                        session, current.task_id, "invocation:" + current.id, [(started, now())]
                    )
                    await session.execute(
                        update(Task)
                        .where(Task.id == current.task_id)
                        .values(
                            total_tokens=Task.total_tokens + tokens,
                        )
                    )
                current.status, current.output_json, current.error_json = (
                    status,
                    redact(output),
                    redact(error),
                )
                current.lease_token, current.lease_expires_at = None, None
                if current.task_id:
                    emit(
                        session,
                        current.task_id,
                        "invocation." + status,
                        {"id": current.id, "output": output, "error": error},
                    )
        return True
