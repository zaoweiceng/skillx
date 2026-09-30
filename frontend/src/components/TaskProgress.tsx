import { useEffect, useState } from 'react'
import { Alert, Collapse, Spin } from 'antd'
import { request } from '@/api/http'
import styles from './TaskProgress.module.css'

import { progressLabel, summarizeProgress, kindLabels, type TaskEvent } from './taskProgressEvents'

export default function TaskProgress({ id, finished, learningActive = false, invocations }: {
  id: string; finished: boolean; learningActive?: boolean; invocations: Array<{id: string; kind: string; status: string}>
}) {
  const [events, setEvents] = useState<TaskEvent[]>([])
  const [error, setError] = useState('')
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    let active = true
    let cursor = 0
    let timer: ReturnType<typeof setTimeout>
    const abort = new AbortController()
    setEvents([])
    setError('')
    async function poll() {
      let more = false
      try {
        const page = await request<TaskEvent[]>(`/tasks/${id}/events?after=${cursor}`, { signal: abort.signal })
        if (!active) return
        if (page.length) {
          cursor = page[page.length - 1].id
          setEvents(current => [...current, ...page])
        }
        more = page.length === 500
        setError('')
      } catch (reason) {
        if (!active) return
        setError(reason instanceof Error ? reason.message : '无法获取进度')
        more = true
      }
      if (active && (!finished || learningActive || more)) timer = setTimeout(poll, more ? 250 : 1000)
    }
    void poll()
    const clock = finished ? undefined : setInterval(() => setNow(Date.now()), 1000)
    return () => { active = false; abort.abort(); clearTimeout(timer); clearInterval(clock) }
  }, [id, finished, learningActive])
  const steps = summarizeProgress(events)
  const latest = events.map(event => ({ ...event, label: progressLabel(event) })).filter(event => event.label).at(-1)
  const waiting = latest ? Math.max(0, Math.floor((now - Date.parse(latest.createdAt)) / 1000)) : 0
  const running = invocations.filter(call => ['running', 'waiting_execution'].includes(call.status))
  const timeline = <ol className={styles.steps}>{steps.map(event => <li key={event.id}>
    <time>{new Date(event.createdAt).toLocaleTimeString()}</time><span>{event.label}{event.detail && <small className={styles.detail}>{event.detail}</small>}</span>
  </li>)}</ol>
  return <div className={styles.progress} aria-label="执行进度">
    {error && <Alert type="warning" title="进度连接暂时中断，正在重连" description={error} />}
    {!finished && <>
      <div className={styles.current} role="status"><Spin size="small" />
        <span>{running.length ? `正在执行：${[...new Set(running.map(call => kindLabels[call.kind] ?? call.kind))].join('、')}` : latest?.label ?? '正在读取执行进度…'}</span>
      </div>
      {waiting >= 15 && <p className={styles.waiting}>距上次进展 {waiting} 秒，{running.length ? '调用尚未返回结果' : '尚未收到新的执行事件'}。</p>}
      {steps.length > 0 && <div className={styles.recent}><ol className={styles.steps}>{steps.slice(-4).map(event => <li key={event.id}>
        <time>{new Date(event.createdAt).toLocaleTimeString()}</time><span>{event.label}{event.detail && <small className={styles.detail}>{event.detail}</small>}</span>
      </li>)}</ol></div>}
    </>}
    {steps.length > 0 && <Collapse ghost size="small" items={[{ key: 'progress', label: finished ? `执行过程 · ${steps.length} 个阶段` : '查看完整执行过程', children: timeline }]} />}
  </div>
}
