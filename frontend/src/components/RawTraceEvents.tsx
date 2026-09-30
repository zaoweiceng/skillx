import { Input, Select } from 'antd'
import { ChevronRight, FileJson, Search } from 'lucide-react'
import { useMemo, useState } from 'react'
import { TraceCodeBlock } from './TraceCodeBlock'
import type { TaskEvent } from './taskProgressEvents'
import styles from './RawTraceEvents.module.css'

function describe(event: TaskEvent) {
  const type = event.type === 'agent.audit' ? String(event.payload.type ?? event.type) : event.type
  const payload = event.type === 'agent.audit' ? event.payload.payload as Record<string, unknown> | undefined : event.payload
  const category = /^(model|agent)\./.test(type) ? 'model' : /^(invocation|tool)\./.test(type) ? 'operation' : /^learning\./.test(type) ? 'learning' : 'task'
  let detail = typeof payload?.model === 'string' ? payload.model : ''
  if (type === 'model.trace') detail = `${payload?.phase === 'request' ? '请求' : '返回'}片段 ${Number(payload?.chunkIndex ?? 0) + 1}/${payload?.chunkCount ?? '?'} `
  return { type, category, detail }
}
function EventRow({ event }: { event: TaskEvent }) {
  const [open, setOpen] = useState(false)
  const { type, category, detail } = describe(event)
  return <details className={styles.event} onToggle={e => setOpen(e.currentTarget.open)}>
    <summary><ChevronRight size={12} className={styles.chevron} />
      <span className={styles.eventInfo}><span className={styles.eventType} title={type}>{type}</span>{detail && <small>{detail}</small>}</span>
      <span className={styles.badge} data-category={category}>{({ model: '模型', operation: '调用', learning: '固化', task: '任务' })[category]}</span>
      <time>{new Date(event.createdAt).toLocaleTimeString()}</time>
    </summary>
    {open && <div className={styles.payload}><TraceCodeBlock label={`事件 #${event.id}`} text={JSON.stringify(event.payload, null, 2)} /></div>}
  </details>
}
export function RawTraceEvents({ events }: { events: TaskEvent[] }) {
  const [query, setQuery] = useState('')
  const [category, setCategory] = useState('all')
  const filtered = useMemo(() => events.filter(event => {
    const description = describe(event)
    return (category === 'all' || description.category === category) &&
      `${description.type} ${description.detail} ${event.id}`.toLowerCase().includes(query.toLowerCase())
  }), [events, category, query])
  return <details className={styles.raw}>
    <summary className={styles.heading}><ChevronRight size={14} className={styles.chevron} /><FileJson size={14} /><span>原始事件</span><small>{events.length}</small></summary>
    <div className={styles.content}>
      <div className={styles.toolbar}>
        <Input size="small" aria-label="搜索原始事件" prefix={<Search size={12} />} placeholder="搜索事件类型" allowClear value={query} onChange={e => setQuery(e.target.value)} />
        <Select size="small" aria-label="原始事件分类" value={category} onChange={setCategory} options={[
          { value: 'all', label: '全部' }, { value: 'model', label: '模型' }, { value: 'operation', label: '调用' }, { value: 'task', label: '任务' }, { value: 'learning', label: '固化' },
        ]} />
      </div>
      <div className={styles.list}>{filtered.map(event => <EventRow key={event.id} event={event} />)}
        {!filtered.length && <p className={styles.empty}>没有匹配的事件</p>}</div>
      <div className={styles.footer}>显示 {filtered.length} / {events.length} 条事件</div>
    </div>
  </details>
}
