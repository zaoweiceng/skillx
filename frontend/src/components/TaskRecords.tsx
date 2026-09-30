import { Button, Collapse, Empty, Tooltip } from 'antd'
import { Download, Terminal, Workflow, Wrench } from 'lucide-react'
import { Link } from 'react-router-dom'
import { useState } from 'react'
import { request } from '@/api/http'
import styles from './TaskRecords.module.css'
import { learningRoleLabels } from './taskProgressEvents'

type Invocation = { id: string; kind: string; status: string; runId?: string; output: unknown; error: unknown; request?: Record<string, unknown>; reason?: string; workingDirectory?: string }
type Learning = { id: string; kind: string; status: string; result: unknown }
const statusLabels: Record<string, string> = { succeeded: '已完成', failed: '失败', cancelled: '已取消', running: '执行中', queued: '等待执行', published: '已发布', blocked: '固化受阻', skipped: '已跳过' }
const kindLabels: Record<string, string> = { code: '代码执行', workflow: '工作流', skillx: 'SkillX 能力', tool: '工具调用', install: '安装依赖', model: '模型推理' }
function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}
function format(value: unknown) {
  if (typeof value === 'string') {
    try { return JSON.stringify(JSON.parse(value), null, 2) } catch { return value }
  }
  return JSON.stringify(value, null, 2) ?? ''
}
function ResultPreview({ value }: { value: unknown }) {
  let parsed = value
  if (typeof value === 'string') { try { parsed = JSON.parse(value) } catch { /* plain output */ } }
  const entries = Object.entries(record(parsed))
  if (entries.length) return <div className={styles.resultList}>{entries.slice(0, 8).map(([key, content]) => {
    const paths = Array.isArray(content) && content.every(x => typeof x === 'string' && /^[DFS]\t/.test(x))
    return <div className={styles.resultRow} key={key}>
      <span className={styles.resultKey}>{{path: '文件路径', content: '文件内容', bytes: '文件大小（字节）', cwd: '执行目录'}[key] ?? key}</span>
      <span>{paths ? `目录清单 · ${content.length} 项` : Array.isArray(content) ? `${content.length} 项结果` : typeof content === 'string' && content.length > 240 ? `已返回文本 · ${content.length} 字符` : format(content).slice(0, 240)}</span>
    </div>
  })}{entries.length > 8 && <span>另有 {entries.length - 8} 项，请展开完整输出查看</span>}</div>
  const text = format(parsed)
  if ([...text.slice(0, 1000)].filter(char => char === '\ufffd' || (char.charCodeAt(0) < 32 && !'\n\r\t'.includes(char))).length > 3) return <div className={styles.reason}>输出含非文本字符，已折叠；可展开查看完整内容。</div>
  return <div className={styles.reason}>{text.slice(0, 240)}{text.length > 240 ? '…（展开查看完整输出）' : ''}</div>
}
function LearningStages({ value }: { value: unknown }) {
  const stages = Object.entries(record(value))
  if (!stages.length) return null
  const labels: Record<string, string> = { running: '执行中', completed: '已完成', failed: '执行失败', needs_revision: '等待修订' }
  const tokens = new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 })
  return <div className={styles.stages} aria-label="后台专业 Agent 阶段">{stages.map(([key, raw]) => {
    const stage = record(raw)
    const invalid = stage.status === 'failed' || stage.status === 'needs_revision'
    return <div className={styles.stage} key={key}>
      <div className={styles.heading}><strong>{learningRoleLabels[String(stage.role)] ?? String(stage.role)}</strong><span className={invalid ? styles.error : styles.status}>{labels[String(stage.status)] ?? String(stage.status)}</span></div>
      <div className={styles.stageMeta}>
        {!!stage.name && <span>{String(stage.name)}</span>}
        {typeof stage.attempt === 'number' && stage.attempt > 1 && <span>第 {stage.attempt} 次尝试</span>}
        {typeof stage.totalTokens === 'number' && <span>{tokens.format(stage.totalTokens)} Token</span>}
        {typeof stage.durationMs === 'number' && <span>{(stage.durationMs / 1000).toFixed(1)} 秒</span>}
        {!!stage.validationStatus && <span>{({ passed: '执行验证通过', running: '执行验证中', failed: '执行验证失败' } as Record<string, string>)[String(stage.validationStatus)]}</span>}
      </div>
      {invalid && !!stage.error && <p className={styles.error}>{String(stage.error)}</p>}
      {stage.output != null && <Collapse ghost size="small" items={[{ key: 'draft', label: '查看阶段产物', children: <pre className={styles.output}>{format(stage.output)}</pre> }]} />}
    </div>
  })}</div>
}
export default function TaskRecords({ id, invocations, learning }: { id: string; invocations: Invocation[]; learning: Learning[] }) {
  const [downloading, setDownloading] = useState(false)
  const [error, setError] = useState('')
  async function download() {
    setDownloading(true); setError('')
    try {
      const data = await request(`/tasks/${id}/session-export`)
      const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }))
      const link = document.createElement('a')
      link.href = url; link.download = `skillx-task-${id}.json`
      document.body.appendChild(link); link.click(); link.remove()
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (reason) { setError(reason instanceof Error ? reason.message : '下载失败，请重试') }
    finally { setDownloading(false) }
  }
  return <div className={styles.records}>
    <div className={styles.toolbar}>
      <span>执行记录 · {invocations.length} 次调用</span>
      <Tooltip title="下载任务信息、调用结果及事件记录（JSON），用于排查问题">
        <Button type="text" size="small" icon={<Download size={14} />} loading={downloading} onClick={download}>下载执行记录</Button>
      </Tooltip>
    </div>
    {error && <div role="alert" className={styles.error}>{error}</div>}
    <Collapse ghost size="small" items={[
      {key: 'calls', label: '调用记录', children: invocations.length ? invocations.map(item => {
        const output = record(item.output)
        const args = item.request ?? {}
        const input = record(args.input)
        const result = item.error ? record(item.error).message ?? item.error : output.stdout ?? item.output
        return <div className={styles.call} key={item.id}>
          <div className={styles.heading}>
            {item.kind === 'code' ? <Terminal size={15} /> : item.kind === 'workflow' ? <Workflow size={15} /> : <Wrench size={15} />}
            <strong>{item.kind === 'code' ? `${args.language === 'javascript' ? 'JavaScript' : 'Python'} 脚本` : item.kind === 'tool' ? String(args.capabilityRef ?? '工具调用') : kindLabels[item.kind] ?? item.kind}</strong>
            <span className={item.status === 'failed' ? styles.error : styles.status}>{statusLabels[item.status] ?? item.status}</span>
          </div>
          {item.reason && <div className={styles.purpose}>{item.reason}</div>}
          {item.workingDirectory && <div className={styles.reason}>工作目录：{item.workingDirectory}</div>}
          {typeof input.path === 'string' && <div className={styles.reason}>目标路径：{input.path}</div>}
          {item.kind === 'code' && output.exitCode != null && <div className={styles.reason}>进程退出码：{String(output.exitCode)}{output.exitCode === 0 ? ' · 脚本执行成功' : ''}</div>}
          {result != null && result !== '' && <ResultPreview value={result} />}
          <Collapse ghost size="small" items={[
            ...(typeof args.source === 'string' ? [{key: 'source', label: '查看执行代码', children: <pre className={styles.output}>{args.source}</pre>}] : []),
            ...(result != null && result !== '' ? [{key: 'output', label: '查看完整输出', children: <pre className={styles.output}>{format(result)}</pre>}] : []),
          ]} />
          {item.runId && <Link to={`/runs/${item.runId}`}>查看工作流</Link>}
          <Collapse ghost size="small" items={[{key: 'raw', label: '原始数据', children: <pre className={styles.output}>{format({request: item.request, output: item.output, error: item.error})}</pre>}]} />
        </div>
      }) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无调用记录" />},
      ...(learning.length ? [{key: 'learning', label: '能力提炼与固化', children: learning.map(item => <div className={styles.call} key={item.id}>
        <div className={styles.heading}><Workflow size={15} /><strong>{item.kind === 'repair' ? '工作流修复' : '可复用能力提炼'}</strong><span className={styles.status}>{statusLabels[item.status] ?? item.status}</span></div>
        {typeof record(item.result).reason === 'string' && <div className={styles.reason}>{String(record(item.result).reason)}</div>}
        {typeof record(item.result).scope === 'string' && <div className={styles.reason}>复用边界：{String(record(item.result).scope)}</div>}
        {Object.keys(record(record(item.result).skills)).length > 0 && <p>Skill：{Object.keys(record(record(item.result).skills)).join('、')}</p>}
        {Object.keys(record(record(item.result).skillx)).length > 0 && <p>SkillX：{Object.keys(record(record(item.result).skillx)).join('、')}</p>}
        <LearningStages value={record(item.result).agentStages} />
        <Collapse ghost size="small" items={[{key: 'raw', label: '原始数据', children: <pre className={styles.output}>{format(item.result)}</pre>}]} />
      </div>)}] : []),
    ]} />
  </div>
}
