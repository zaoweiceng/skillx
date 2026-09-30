import MarkdownContent from './MarkdownContent'
import { useEffect, useRef, useState } from 'react'
import { Alert, Button } from 'antd'
import { request } from '@/api/http'
import TaskRecords from './TaskRecords'
import TaskProgress from './TaskProgress'
import HumanInteractionCard, { type HumanInteraction } from './HumanInteractionCard'

type Task = { id: string; status: string; output: {text?: string}; state: {summary?: string; remaining?: string[]};
  error?: {message?: string}; invocations: Array<{id: string; kind: string; status: string; runId?: string; output: unknown; error: unknown}>;
  interactions: HumanInteraction[]; approvals: Array<{id: string; snapshotDigest: string; runId: string; snapshot: {title?: string; description?: string}}>;
  learning: Array<{id: string; kind: string; status: string; result: unknown}> }
export default function TaskPanel({ id, onFinished }: { id: string; onFinished?: () => Promise<void> }) {
  const [task, setTask] = useState<Task>()
  const [error, setError] = useState('')
  const notified = useRef('')
  const [revision, setRevision] = useState(0)
  useEffect(() => {
    let active = true
    async function load() { try { const value = await request<Task>(`/tasks/${id}`); if (active) { setTask(value); setError('') } } catch (e) { if (active) setError(String(e)) } }
    void load(); const timer = setInterval(load, 1500)
    return () => { active = false; clearInterval(timer) }
  }, [id, revision])
  useEffect(() => {
    if (task && ['succeeded', 'failed', 'cancelled', 'blocked'].includes(task.status) && notified.current !== id) {
      notified.current = id
      void onFinished?.().catch(reason => setError(String(reason)))
    }
  }, [task, id, onFinished])
  async function cancel() { try { await request(`/tasks/${id}/cancel`, {method: 'POST'}); setRevision(x => x + 1) } catch (e) { setError(String(e)) } }
  const finished = !!task && ['succeeded', 'failed', 'cancelled', 'blocked'].includes(task.status)
  return <section className="task-panel" aria-label="任务执行">
    {error && <Alert type="error" title={error} />}
    <span className="task-state">{({succeeded: '已完成', failed: '执行失败', cancelled: '已取消', blocked: '等待处理', running: '正在处理', deciding: '正在分析任务', waiting_execution: '正在执行调用', waiting_user: '等待你的回复', queued: '等待执行'} as Record<string, string>)[task?.status ?? 'queued'] ?? task?.status}</span>
    {task?.output.text && <MarkdownContent>{task.output.text}</MarkdownContent>}
    <TaskProgress id={id} finished={finished} learningActive={task?.learning.some(job => ['running', 'queued'].includes(job.status))} invocations={task?.invocations ?? []} />
    {task?.error && <Alert type="error" title={task.error.message ?? '任务执行失败'} />}
    {task?.interactions.map(item => <HumanInteractionCard key={item.id} item={item} onAnswered={() => setRevision(x => x + 1)} />)}
    {task && !['succeeded', 'failed', 'cancelled', 'blocked'].includes(task.status) && <Button onClick={cancel}>取消任务</Button>}
    <TaskRecords id={id} invocations={task?.invocations ?? []} learning={task?.learning ?? []} />
  </section>
}
