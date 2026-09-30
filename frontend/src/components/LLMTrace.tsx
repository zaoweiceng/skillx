import { Collapse, Tabs } from 'antd'
import { Brain, ChevronRight, Clock3, Coins } from 'lucide-react'
import MarkdownContent from './MarkdownContent'
import { TraceCodeBlock } from './TraceCodeBlock'
import { collectModelCalls, readTraceBody, type ModelCall, type TraceEvent } from './llmTraceData'
import styles from './LLMTrace.module.css'

const number = new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 })
const tokens = (value: unknown) => typeof value === 'number' ? number.format(value) : '—'
const duration = (value: unknown) => typeof value === 'number' ? `${(value / 1000).toFixed(1)}s` : '等待返回'
function CallDetails({ call }: { call: ModelCall }) {
  const response = readTraceBody(call, 'response')
  const content = Array.isArray(response.data?.content) ? response.data.content as Array<Record<string, unknown>> : []
  const thinking = content.filter(item => item.type === 'thinking' && typeof item.thinking === 'string').map(item => item.thinking).join('\n\n')
  const fields = [['输入', call.metadata.inputTokens], ['输出', call.metadata.outputTokens],
    ['缓存读取', call.metadata.cacheReadTokens], ['缓存写入', call.metadata.cacheWriteTokens]]
  return <div className={styles.callBody}>
    <div className={styles.metrics}>{fields.map(([label, value]) => <div key={String(label)}><span>{String(label)}</span><strong>{tokens(value)}<small>Token</small></strong></div>)}</div>
    {call.metadata.usageComplete === false && <p className={styles.notice}>服务未完整上报本轮用量</p>}
    <Tabs size="small" className={styles.tabs} items={[
      { key: 'thinking', label: '思考过程', children: <div className={styles.thinking}>
        {thinking ? <MarkdownContent>{thinking}</MarkdownContent> : <p className={styles.empty}>{response.data ? '本轮模型未返回独立的思考内容' : '等待模型返回思考内容…'}</p>}
      </div> },
      { key: 'request', label: '模型请求', children: <TraceCodeBlock label="请求 JSON" text={readTraceBody(call, 'request').text} /> },
      { key: 'response', label: '模型返回', children: <TraceCodeBlock label="返回 JSON" text={response.text} /> },
    ]} />
  </div>
}
export function LLMTrace({ events, embedded = false, callNumbers }: { events: readonly TraceEvent[]; embedded?: boolean; callNumbers?: ReadonlyMap<string, number> }) {
  const calls = collectModelCalls(events)
  if (!calls.length) return null
  return <section aria-label={embedded ? '本阶段模型调用' : 'LLM 执行轨迹'} className={styles.trace}>
    {!embedded && <h3 className={styles.heading}><Brain size={15} />模型调用<span>{calls.length}</span></h3>}
    <Collapse size="small" className={styles.collapse} expandIcon={({ isActive }) => <ChevronRight size={14} className={isActive ? styles.expanded : styles.chevron} />}
      items={calls.map((call, index) => ({ key: call.id,
        label: <div className={styles.callHeading}><div className={styles.callTitle}>
          <Brain size={14} /><span>第 {callNumbers?.get(call.id) ?? index + 1} 次模型调用</span>
          <small title={String(call.metadata.model ?? 'Pi')}>{String(call.metadata.model ?? 'Pi')}</small>
        </div><div className={styles.callMeta}>
          <span><Clock3 size={11} />{duration(call.metadata.durationMs)}</span>
          {typeof call.metadata.inputTokens === 'number' && typeof call.metadata.outputTokens === 'number' && <span><Coins size={11} />{tokens(call.metadata.inputTokens + call.metadata.outputTokens)} Token</span>}
          {Number(call.metadata.status) >= 400 && <span className={styles.error}>HTTP {String(call.metadata.status)}</span>}
        </div></div>,
        children: <CallDetails call={call} />,
      }))} />
  </section>
}
