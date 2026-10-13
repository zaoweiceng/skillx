"""Start Agent-led extraction in its own conversation using a bounded source snapshot."""
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select

from app.application.runtime_profiles import agent_snapshot
from app.application.tasks import emit, finish_task
from app.config import ExtractionBudget, get_settings
from app.infrastructure.database import Conversation, ConversationTurn, Message, utcnow
from app.infrastructure.models.tasks import HumanInteraction, Invocation, LearningJob, Task, TaskEvent


async def start_extraction(session, source_id, budget: ExtractionBudget | None = None):
    source = await session.get(Conversation, source_id)
    if source is None or source.deleted_at is not None:
        raise HTTPException(404, "对话不存在")
    if budget is None:
        budget = get_settings().extraction_source_budgets.get(source_id)
    tasks = list(await session.scalars(select(Task).where(Task.conversation_id == source.id)))
    ids = [task.id for task in tasks]
    calls = list(await session.scalars(select(Invocation).where(
        Invocation.task_id.in_(ids), Invocation.status.in_(["succeeded", "failed"])
    ).order_by(Invocation.created_at)))
    messages = list(await session.scalars(select(Message).where(
        Message.conversation_id == source.id).order_by(Message.created_at)))
    if not messages:
        raise HTTPException(400, "对话暂无可提取内容")
    answers = list(await session.scalars(select(HumanInteraction).where(HumanInteraction.task_id.in_(ids))))
    decisions = list(await session.scalars(select(TaskEvent).where(
        TaskEvent.task_id.in_(ids), TaskEvent.type == "task.decision").order_by(TaskEvent.id)))
    now = utcnow()
    conversation = Conversation(id=str(uuid4()), title=("提取工作流 · " + source.title)[:200],
        working_directory=source.working_directory, agent_profile_id=source.agent_profile_id,
        model_profile_id=source.model_profile_id, agent_model=source.agent_model,
        traex_model=source.traex_model, reasoning_effort=source.reasoning_effort,
        workflow_mode=source.workflow_mode, last_message_at=now)
    session.add(conversation)
    await session.flush()
    turn = ConversationTurn(id=str(uuid4()), conversation_id=conversation.id,
        workflow_mode_snapshot=source.workflow_mode, status="processing", accepted_at=now,
        first_response_at=now)
    session.add(turn)
    await session.flush()
    task = Task(id=str(uuid4()), conversation_id=conversation.id, turn_id=turn.id, status="learning",
        goal=("先比较已有 Skill、SkillX 和工作流，功能已覆盖则直接复用；"
              "仅提取缺失能力，部分覆盖时依赖已有能力实现增量。仅提炼，不重新执行来源任务。"),
        runtime_json=await agent_snapshot(session, source.agent_profile_id, source.traex_model,
            source.reasoning_effort, source.model_profile_id, source.agent_model),
        state_json={"workingDirectory": source.working_directory,
            "extractionRequirements": {"requireWorkflowComposition": bool(budget and budget.requireWorkflowComposition)},
            "extractionSource": {
            "conversationId": source.id, "title": source.title,
            "invocationIds": [call.id for call in calls],
            "sessionMessages": [{"role": item.role, "content": item.content_json} for item in messages],
            "humanAnswers": [{"kind": item.kind, "request": item.request_json, "answer": item.answer_json} for item in answers],
            "decisions": [item.payload_json for item in decisions],
            "outcome": {"tasks": [{"goal": item.goal, "status": item.status, "output": item.output_json} for item in tasks]},
        }})
    session.add(task)
    await session.flush()
    user = Message(id=str(uuid4()), conversation_id=conversation.id, turn_id=turn.id, role="user",
        content_json={"type": "text", "text": f"从对话「{source.title}」提取独立能力与可组合工作流。"})
    assistant = Message(id=str(uuid4()), conversation_id=conversation.id, turn_id=turn.id, role="assistant",
        content_json={"type": "task", "taskId": task.id, "text": "正在提取能力与工作流"})
    session.add_all([user, assistant])
    await session.flush()
    turn.user_message_id, turn.assistant_message_id = user.id, assistant.id
    session.add(LearningJob(id=str(uuid4()), task_id=task.id, kind="distill",
        result_json={"budget": {**budget.model_dump(), "startedAt": now.isoformat()}} if budget else {}))
    emit(session, task.id, "learning.queued", {"sourceConversationId": source.id})
    await session.commit()
    return {"conversationId": conversation.id, "taskId": task.id}


async def finish_extraction(session, task_id, status, output):
    task = await session.get(Task, task_id)
    if not task.state_json.get("extractionSource") or task.status == "cancelled":
        return
    if status in {"queued", "running"}:
        return
    if status == "partial":
        summary = "提取部分完成，仍有产物未发布。"
    elif status == "published":
        summary = "提取已完成。"
    elif output.get("reusedArtifacts"):
        summary = "已复用现有能力，无需重复创建。"
    else:
        summary = "本次提取未发布新产物。"
    lines = [summary]
    for key, label in [("skills", "操作指南"), ("skillx", "独立能力"), ("workflows", "工作流")]:
        names = [name for name in output.get(key, {})
                 if name not in output.get("reusedArtifacts", {}).get(key, {})]
        if names:
            lines.append(f"\n{label}：" + "、".join(names))
    for key, label in [("skills", "操作指南"), ("skillx", "独立能力"), ("workflows", "工作流")]:
        names = list(output.get("reusedArtifacts", {}).get(key, {}))
        if names:
            lines.append(f"\n已复用{label}（未重复创建）：" + "、".join(names))
    failures = output.get("failures", {})
    if failures:
        lines.append("\n以下产物未发布，已发布的产物可独立使用：")
        lines.extend(f"- {name}：{reason}" for name, reason in failures.items())
    await finish_task(session, task, "succeeded" if status in {"published", "partial", "skipped"} else "blocked", "\n".join(lines))
