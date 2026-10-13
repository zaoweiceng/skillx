import { useRef, useState } from 'react'
import { Alert, Button, Card, Tag } from 'antd'
import Form from '@/components/SchemaForm'
import validator from '@rjsf/validator-ajv8'
import HumanAnswerSummary from './HumanAnswerSummary'
import { request } from '@/api/http'
import { rjsfSchema } from '@/lib/utils'

export type HumanInteraction = {
  id: string; kind: string; status: string; revision: number; snapshotDigest: string
  request: { approvalId?: string; operation?: Record<string, unknown>; title: string; reason: string; inputSchema: Record<string, unknown>; uiSchema?: Record<string, unknown>; recommended?: string; options?: Array<{value?: unknown; label?: string; description?: string}> }
  answer?: Record<string, unknown>
}
export default function HumanInteractionCard({ item, onAnswered }: { item: HumanInteraction; onAnswered: () => void }) {
  const [busy, setBusy] = useState(false)
  const [submittedAnswer, setSubmittedAnswer] = useState<Record<string, unknown>>()
  const pending = item.status === 'pending' && !submittedAnswer
  const answer = item.answer ?? submittedAnswer
  const [error, setError] = useState('')
  const answerKey = useRef(crypto.randomUUID())
  async function submit(answer: Record<string, unknown>) {
    setBusy(true); setError('')
    try {
      await request(`/human-interactions/${item.id}/answer`, {method: 'POST', body: JSON.stringify({revision: item.revision, snapshotDigest: item.snapshotDigest, idempotencyKey: answerKey.current, answer})})
      setSubmittedAnswer(answer)
      onAnswered()
    } catch (reason) { setError(String(reason)) } finally { setBusy(false) }
  }
  return <Card size="small" title={item.request.title} extra={<Tag>{pending ? '需要你的决定' : answer ? '已回答' : item.status === 'cancelled' ? '已取消' : '已失效'}</Tag>}>
    {pending && <><p>{item.request.reason}</p>
    {item.request.operation && <pre>{JSON.stringify(item.request.operation, null, 2)}</pre>}
    {item.request.recommended && <p>建议：{item.request.recommended}（需你提交）</p>}
    {item.request.options?.map((option, i) => <p key={i}><strong>{option.label}</strong> {option.description}</p>)}</>}
    {error && <Alert type="error" title={error} />}
    {pending ? item.kind === 'authorization' ? <div>
      <Button loading={busy} type="primary" onClick={() => submit({approved: true})}>批准本次操作</Button>
      <Button disabled={busy} onClick={() => submit({approved: false})}>拒绝</Button>
    </div> : <Form schema={rjsfSchema(item.request.inputSchema)} uiSchema={item.request.uiSchema ?? {}} validator={validator} disabled={busy}
      onSubmit={event => submit((event.formData ?? {}) as Record<string, unknown>)}><Button htmlType="submit" type="primary" loading={busy}>提交并继续</Button></Form>
      : answer ? <HumanAnswerSummary answer={answer} schema={item.request.inputSchema} ui={item.request.uiSchema} options={item.request.options} />
        : <p>{item.status === 'cancelled' ? '此决定已取消，无需操作。' : '此决定已失效，无需操作。'}</p>}
  </Card>
}
