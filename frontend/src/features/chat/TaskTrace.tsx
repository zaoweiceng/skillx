import { useEffect, useMemo, useState } from 'react'
import { Alert } from 'antd'
import { Brain, CheckCheck, Wrench } from 'lucide-react'
import { request } from '@/api/http'
import styles from './TaskTrace.module.css'
import { taskTraceSteps } from './taskTraceSteps'
import { LLMTrace } from '@/components/LLMTrace'
import { collectModelCalls } from '@/components/llmTraceData'
import { RawTraceEvents } from '@/components/RawTraceEvents'

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
  const steps = useMemo(() => taskTraceSteps(events), [events])
  const modelCalls = useMemo(() => collectModelCalls(events), [events])
  const callNumbers = new Map(modelCalls.map((call, index) => [call.id, index + 1]))
  return <div className={styles.trace} aria-label="任务执行轨迹">
    {error && <Alert type="error" title={error} />}
    {!events.length && !error && <p className="hint">正在读取任务轨迹…</p>}
    <div className={styles.summary}>
      <span><Brain size={12} /><strong>{modelCalls.length}</strong>轮分析</span>
      <span><Wrench size={12} /><strong>{calls.size}</strong>项操作</span>
      <span><CheckCheck size={12} /><strong>{results.size}</strong>项已返回</span>
    </div>
    <ol className={styles.steps}>{steps.map((step, index) => <li key={step.id}>
      <span className={styles.marker}>{index + 1}</span><div className={styles.content}>
        <div className={styles.heading}><span>{step.label}</span><time>{new Date(step.createdAt).toLocaleTimeString()}</time></div>
        {step.detail && <p className={styles.detail}>{step.detail}</p>}
        <LLMTrace events={step.events} embedded callNumbers={callNumbers} />
      </div>
    </li>)}</ol>
    {events.length > 0 && <RawTraceEvents events={events} />}
  </div>
}
