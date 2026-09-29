import { useEffect, useState } from 'react'
import { Alert, Button, Select, Tag } from 'antd'
import Form from '@rjsf/core'
import validator from '@rjsf/validator-ajv8'
import { Link, useParams } from 'react-router-dom'
import { request } from '@/api/http'
import { rjsfSchema } from '@/lib/utils'
import HumanInteractionCard, { type HumanInteraction } from '@/components/HumanInteractionCard'

type Version = {id: string; version: string; interfaces: Record<string, {uiSchema?: Record<string, unknown>; inputSchema: Record<string, unknown>; outputSchema: Record<string, unknown>}>}
type Invocation = {id: string; status: string; output: unknown; error: unknown; runId?: string; interactions?: HumanInteraction[]}
export default function CapabilityDetailPage() {
  const {id} = useParams()
  const [versions, setVersions] = useState<Version[]>([])
  const [versionId, setVersionId] = useState('')
  const [iface, setIface] = useState('')
  const [runs, setRuns] = useState<Invocation[]>([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [jsonMode, setJsonMode] = useState(false)
  const [json, setJson] = useState('{}')
  const [revision, setRevision] = useState(0)
  useEffect(() => { let active = true; request<Version[]>(`/capabilities/${id}/versions`).then(items => {if (active) {setVersions(items); setVersionId(items[0]?.id ?? ''); setIface(Object.keys(items[0]?.interfaces ?? {})[0] ?? '')}}).catch(e => setError(String(e))); return () => {active = false} }, [id])
  useEffect(() => { if (!versionId) return; let active = true; const load = () => request<Invocation[]>(`/invocations?versionId=${versionId}`).then(items => {if (active) setRuns(items)}).catch(e => {if (active) setError(String(e))}); void load(); const timer = setInterval(load, 1500); return () => {active = false; clearInterval(timer)} }, [versionId, revision])
  const version = versions.find(v => v.id === versionId)
  const contract = version?.interfaces[iface]
  async function execute(input: unknown) {setBusy(true); setError(''); try {await request(`/capability-versions/${versionId}/invocations`, {method: 'POST', body: JSON.stringify({interface: iface, input, idempotencyKey: crypto.randomUUID()})}); setRevision(x => x + 1)} catch (e) {setError(String(e))} finally {setBusy(false)}}
  return <div className="page"><Link to="/capabilities">返回能力列表</Link><h1>能力手动执行</h1>
    {error && <Alert type="error" title={error} />}
    <Select aria-label="能力版本" value={versionId || undefined} options={versions.map(v => ({value: v.id, label: v.version}))} onChange={value => {setVersionId(value); setIface(Object.keys(versions.find(v => v.id === value)?.interfaces ?? {})[0] ?? '')}} />
    <Select aria-label="能力接口" value={iface || undefined} options={Object.keys(version?.interfaces ?? {}).map(value => ({value, label: value}))} onChange={setIface} />
    <Button onClick={() => setJsonMode(x => !x)}>{jsonMode ? '表单输入' : 'JSON 输入'}</Button>
    {contract && (jsonMode ? <div><textarea aria-label="JSON 参数" value={json} onChange={event => setJson(event.target.value)} /><Button loading={busy} onClick={() => {try {void execute(JSON.parse(json))} catch(e) {setError(String(e))}}}>运行</Button></div> : <Form key={`${versionId}:${iface}`} schema={rjsfSchema(contract.inputSchema)} uiSchema={contract.uiSchema ?? {}} validator={validator} onSubmit={event => execute(event.formData)}><Button htmlType="submit" type="primary" loading={busy}>运行能力</Button></Form>)}
    <h2>运行历史</h2>{runs.map(run => <section className="panel" key={run.id}><Tag>{run.status}</Tag><code>{run.id}</code>{run.runId && <Link to={`/runs/${run.runId}`}>运行详情</Link>}
      {run.interactions?.map(item => <HumanInteractionCard key={item.id} item={item} onAnswered={() => setRevision(x => x + 1)} />)}
      {!['succeeded','failed','cancelled'].includes(run.status) && <Button onClick={() => request(`/invocations/${run.id}/cancel`, {method: 'POST'}).then(() => setRevision(x => x + 1)).catch(e => setError(String(e)))}>取消</Button>}
      <pre>{JSON.stringify(run.error ?? run.output, null, 2)}</pre></section>)}
  </div>
}
