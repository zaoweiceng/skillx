import { useEffect, useState } from 'react'
import { Alert, Collapse } from 'antd'
import { request } from '@/api/http'
import styles from '@/components/TaskProgress.module.css'
import { summarizeProgress } from '@/components/taskProgressEvents'

type Event = { id: number; type: string; payload: Record<string, unknown>; createdAt: string }
export function TaskTrace({ id }: { id: string }) {
  const [events, setEvents] = useState<Event[]>([])
  const [error, setError] = useState('')
  useEffect(() => {
    let active = true
    let cursor = 0
    let timer: ReturnType<typeof setTimeout>
    setEvents([])
    async function poll() {
      try {
        const page = await request<Event[]>(`/tasks/${id}/events?after=${cursor}`)
        if (!active) return
        if (page.length) { cursor = page[page.length - 1].id; setEvents(current => [...current, ...page]) }
        setError('')
      } catch (reason) { if (active) setError(reason instanceof Error ? reason.message : '轨迹加载失败') }
      if (active) timer = setTimeout(poll, 1500)
    }
    void poll()
    return () => { active = false; clearTimeout(timer) }
  }, [id])
  const calls = new Set(events.filter(event => event.type === 'invocation.queued').map(event => event.payload.id))
  const results = new Set(events.filter(event => ['invocation.succeeded', 'invocation.failed'].includes(event.type)).map(event => event.payload.id))
  const steps = summarizeProgress(events)
  return <div className={styles.trace} aria-label="任务执行轨迹">
    {error && <Alert type="error" title={error} />}
    {!events.length && !error && <p className="hint">正在读取任务轨迹…</p>}
    <p className={styles.summary}>{calls.size ? `${calls.size} 项操作 · ${results.size} 项已返回` : '等待实际操作'}</p>
    <ol className={styles.traceSteps}>{steps.map(step => <li key={step.id}>
      <div className={styles.stepHeading}><span>{step.label}</span><time>{new Date(step.createdAt).toLocaleTimeString()}</time></div>
      {step.detail && <p className={styles.detail}>{step.detail}</p>}
    </li>)}</ol>
    {events.length > 0 && <Collapse ghost size="small" items={[{
      key: 'raw', label: `原始事件 · ${events.length}`,
      children: <div className={styles.rawEvents}>{events.map(event => <details key={event.id}>
        <summary>{event.type} · {new Date(event.createdAt).toLocaleTimeString()}</summary>
        <pre>{JSON.stringify(event.payload, null, 2)}</pre>
      </details>)}</div>,
    }]} />}
  </div>
}
