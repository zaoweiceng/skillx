import { useEffect, useState } from 'react'
import { Alert, Button, Tag } from 'antd'
import { Link } from 'react-router-dom'
import { API_BASE, request } from '@/api/http'
import HumanInteractionCard, { type HumanInteraction } from './HumanInteractionCard'

type Task = { id: string; status: string; output: {text?: string}; state: {summary?: string; remaining?: string[]};
  error?: {message?: string}; invocations: Array<{id: string; kind: string; status: string; runId?: string; output: unknown; error: unknown}>;
  interactions: HumanInteraction[]; approvals: Array<{id: string; snapshotDigest: string; runId: string; snapshot: {title?: string; description?: string}}>;
  learning: Array<{id: string; kind: string; status: string; result: unknown}> }
export default function TaskPanel({ id }: { id: string }) {
  const [task, setTask] = useState<Task>()
  const [error, setError] = useState('')
  const [revision, setRevision] = useState(0)
  useEffect(() => {
    let active = true
    async function load() { try { const value = await request<Task>(`/tasks/${id}`); if (active) setTask(value) } catch (e) { if (active) setError(String(e)) } }
    void load(); const timer = setInterval(load, 1500)
    return () => { active = false; clearInterval(timer) }
  }, [id, revision])
  async function cancel() { try { await request(`/tasks/${id}/cancel`, {method: 'POST'}); setRevision(x => x + 1) } catch (e) { setError(String(e)) } }
  return <section aria-label="任务执行">
    {error && <Alert type="error" title={error} />}
    <Tag>{task?.status ?? 'queued'}</Tag>
    <p style={{whiteSpace: 'pre-wrap'}}>{task?.output.text || task?.state.summary || 'Agent 正在处理任务…'}</p>
    {task?.error && <Alert type="error" title={task.error.message ?? '任务执行失败'} />}
    {task?.interactions.map(item => <HumanInteractionCard key={item.id} item={item} onAnswered={() => setRevision(x => x + 1)} />)}
    {task && !['succeeded', 'failed', 'cancelled', 'blocked'].includes(task.status) && <Button onClick={cancel}>取消任务</Button>}
    <a href={`${API_BASE}/tasks/${id}/session-export`} target="_blank" rel="noreferrer">导出 session</a>
    <details><summary>调用与审计记录</summary>{task?.invocations.map(item => <div key={item.id}><Tag>{item.kind}</Tag>{item.status}{item.runId && <Link to={`/runs/${item.runId}`}>查看工作流</Link>}<pre>{JSON.stringify(item.error ?? item.output, null, 2)}</pre></div>)}</details>
    {!!task?.learning.length && <details><summary>后台固化与维护</summary><pre>{JSON.stringify(task.learning, null, 2)}</pre></details>}
  </section>
}
