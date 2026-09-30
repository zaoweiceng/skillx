import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import { LLMTrace } from './LLMTrace'
afterEach(cleanup)
it('reassembles persisted chunks and exposes returned thinking beside the request and response', () => {
  const audit = (type: string, payload: Record<string, unknown>) => ({ type: 'agent.audit', payload: { type, payload } })
  render(<LLMTrace events={[
    audit('model.request.dispatched', { requestId: 'call-1', model: 'test-model' }),
    audit('model.trace', { requestId: 'call-1', phase: 'request', chunkIndex: 1, chunkCount: 2, data: '"hello"}' }),
    audit('model.trace', { requestId: 'call-1', phase: 'request', chunkIndex: 0, chunkCount: 2, data: '{"prompt":' }),
    audit('model.trace', { requestId: 'call-1', phase: 'response', chunkIndex: 0, chunkCount: 1,
      data: JSON.stringify({ content: [{ type: 'thinking', thinking: '先读取现有配置，再校验输出。' }, { type: 'text', text: 'ok' }] }) }),
    audit('agent.step.completed', { requestId: 'call-1', inputTokens: 10000, outputTokens: 500, cacheReadTokens: 3, cacheWriteTokens: 0, durationMs: 2000 }),
  ]} />)
  fireEvent.click(screen.getByText('第 1 次模型调用'))
  expect(screen.getByText('先读取现有配置，再校验输出。')).toBeInTheDocument()
  expect(screen.getByText('缓存读取')).toBeInTheDocument()
  expect(screen.getByText('10K')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('tab', { name: '模型请求' }))
  expect(screen.getByText(/"prompt": "hello"/)).toBeInTheDocument()
  fireEvent.click(screen.getByRole('tab', { name: '模型返回' }))
  expect(screen.getByText(/"text": "ok"/)).toBeInTheDocument()
})
