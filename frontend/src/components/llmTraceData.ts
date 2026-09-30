export type TraceEvent = { type: string; payload: Record<string, unknown> }
export type ModelCall = { id: string; metadata: Record<string, unknown>; request: Map<number, string>; response: Map<number, string>; requestCount: number; responseCount: number }
export function collectModelCalls(events: readonly TraceEvent[]): ModelCall[] {
  const calls = new Map<string, ModelCall>()
  for (const envelope of events) {
    const event = envelope.type === 'agent.audit' ? envelope.payload as TraceEvent : envelope
    const payload = event.payload
    if (!payload || typeof payload.requestId !== 'string') continue
    if (!['model.request.dispatched', 'model.trace', 'model.response.started', 'agent.step.completed'].includes(event.type)) continue
    const call = calls.get(payload.requestId) ?? { id: payload.requestId, metadata: {}, request: new Map(), response: new Map(), requestCount: 0, responseCount: 0 }
    if (event.type === 'model.trace' && ['request', 'response'].includes(String(payload.phase))) {
      const phase = payload.phase as 'request' | 'response'
      call[phase].set(Number(payload.chunkIndex), String(payload.data))
      call[phase === 'request' ? 'requestCount' : 'responseCount'] = Number(payload.chunkCount)
    } else call.metadata = { ...call.metadata, ...payload }
    calls.set(call.id, call)
  }
  return [...calls.values()]
}
export function readTraceBody(call: ModelCall, phase: 'request' | 'response'): { text: string; data?: Record<string, unknown> } {
  const chunks = call[phase]
  const expected = call[phase === 'request' ? 'requestCount' : 'responseCount']
  if (!chunks.size) return { text: '等待模型数据…' }
  if (chunks.size !== expected || Array.from({ length: expected }, (_, index) => index).some(index => !chunks.has(index))) return { text: `轨迹正在接收（${chunks.size}/${expected}）` }
  const raw = Array.from({ length: expected }, (_, index) => chunks.get(index)).join('')
  try { const data = JSON.parse(raw); return { text: JSON.stringify(data, null, 2), data } }
  catch { return { text: raw } }
}
