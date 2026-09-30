import { useRef, useState } from 'react'
import { Alert, Button, Card, Tag } from 'antd'
import Form from '@/components/SchemaForm'
import validator from '@rjsf/validator-ajv8'
import { request } from '@/api/http'
import { rjsfSchema } from '@/lib/utils'

export type HumanInteraction = {
  id: string; kind: string; status: string; revision: number; snapshotDigest: string
  request: { approvalId?: string; operation?: Record<string, unknown>; title: string; reason: string; inputSchema: Record<string, unknown>; uiSchema?: Record<string, unknown>; recommended?: string; options?: Array<{label?: string; description?: string}> }
  answer?: Record<string, unknown>
}
export default function HumanInteractionCard({ item, onAnswered }: { item: HumanInteraction; onAnswered: () => void }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const answerKey = useRef(crypto.randomUUID())
  async function submit(answer: Record<string, unknown>) {
    setBusy(true); setError('')
    try {
      await request(`/human-interactions/${item.id}/answer`, {method: 'POST', body: JSON.stringify({revision: item.revision, snapshotDigest: item.snapshotDigest, idempotencyKey: answerKey.current, answer})})
      onAnswered()
    } catch (reason) { setError(String(reason)) } finally { setBusy(false) }
  }
  return <Card size="small" title={item.request.title} extra={<Tag>{item.status === 'pending' ? '需要你的决定' : '已处理'}</Tag>}>
    <p>{item.request.reason}</p>
    {item.request.operation && <pre>{JSON.stringify(item.request.operation, null, 2)}</pre>}
    {item.request.recommended && <p>建议：{item.request.recommended}（需你提交）</p>}
    {item.request.options?.map((option, i) => <p key={i}><strong>{option.label}</strong> {option.description}</p>)}
    {error && <Alert type="error" title={error} />}
    {item.status === 'pending' ? item.kind === 'authorization' ? <div>
      <Button loading={busy} type="primary" onClick={() => submit({approved: true})}>批准本次操作</Button>
      <Button disabled={busy} onClick={() => submit({approved: false})}>拒绝</Button>
    </div> : <Form schema={rjsfSchema(item.request.inputSchema)} uiSchema={item.request.uiSchema ?? {}} validator={validator} disabled={busy}
      onSubmit={event => submit((event.formData ?? {}) as Record<string, unknown>)}><Button htmlType="submit" type="primary" loading={busy}>提交并继续</Button></Form>
      : <pre>{JSON.stringify(item.answer, null, 2)}</pre>}
  </Card>
}
