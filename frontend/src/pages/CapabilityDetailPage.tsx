import { useEffect, useState } from 'react'
import { Alert, Button, Card, Empty, Flex, Input, Select, Tag, Typography } from 'antd'
import { ArrowLeft, Play } from 'lucide-react'
import Form from '@/components/SchemaForm'
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
  return <div className="page capability-detail-page">
    <Link className="back-link" to="/capabilities"><ArrowLeft size={15} />返回能力列表</Link>
    <header className="page-header compact-header">
      <div><span className="eyebrow">工作空间 / 执行</span><h1>能力手动执行</h1><p>选择版本和接口，填写参数后运行，并在下方查看执行记录。</p></div>
    </header>
    {error && <Alert type="error" title={error} showIcon />}
    <Card title="运行配置" className="execution-card">
      <div className="execution-options">
        <label><span>能力版本</span><Select aria-label="能力版本" value={versionId || undefined}
          options={versions.map(v => ({value: v.id, label: `版本 ${v.version}`}))}
          onChange={value => {setVersionId(value); setIface(Object.keys(versions.find(v => v.id === value)?.interfaces ?? {})[0] ?? '')}} /></label>
        <label><span>执行接口</span><Select aria-label="能力接口" value={iface || undefined}
          options={Object.keys(version?.interfaces ?? {}).map(value => ({value, label: value}))} onChange={setIface} /></label>
        <Button onClick={() => setJsonMode(x => !x)}>{jsonMode ? '表单输入' : 'JSON 输入'}</Button>
      </div>
      <div className="execution-input">
        <h2>输入参数</h2>
        {contract ? (jsonMode ? <Flex vertical gap={16}>
          <Input.TextArea aria-label="JSON 参数" value={json} onChange={event => setJson(event.target.value)} autoSize={{minRows: 6, maxRows: 18}} className="json-input" />
          <Button type="primary" loading={busy} icon={<Play size={14} />} style={{alignSelf: 'flex-start'}}
            onClick={() => {try {void execute(JSON.parse(json))} catch(e) {setError(String(e))}}}>运行</Button>
        </Flex> : <Form key={`${versionId}:${iface}`} schema={rjsfSchema(contract.inputSchema)} uiSchema={contract.uiSchema ?? {}} validator={validator} disabled={busy} onSubmit={event => execute(event.formData)}>
          <Button htmlType="submit" type="primary" loading={busy} icon={<Play size={14} />}>运行能力</Button>
        </Form>) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无可运行的接口" />}
      </div>
    </Card>
    <Card title={<h2>运行历史</h2>} extra={<Typography.Text type="secondary">{runs.length} 条记录</Typography.Text>} className="execution-card">
      {runs.length === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无运行记录，提交参数后将在这里展示执行结果" /> : <Flex vertical gap={16}>
        {runs.map(run => <Card size="small" key={run.id} className="invocation-card">
          <Flex align="center" gap={12} wrap>
            <Tag>{run.status}</Tag><Typography.Text className="invocation-id" type="secondary">{run.id}</Typography.Text>
            {run.runId && <Link to={`/runs/${run.runId}`}>运行详情</Link>}
            {!['succeeded','failed','cancelled'].includes(run.status) && <Button size="small" onClick={() => request(`/invocations/${run.id}/cancel`, {method: 'POST'}).then(() => setRevision(x => x + 1)).catch(e => setError(String(e)))}>取消</Button>}
          </Flex>
          {run.interactions?.map(item => <HumanInteractionCard key={item.id} item={item} onAnswered={() => setRevision(x => x + 1)} />)}
          {run.error != null || run.output != null ? <pre className="invocation-output">{JSON.stringify(run.error ?? run.output, null, 2)}</pre> : <Typography.Paragraph type="secondary" className="invocation-pending">等待执行结果</Typography.Paragraph>}
        </Card>)}
      </Flex>}
    </Card>
  </div>
}
