import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import { RawTraceEvents } from './RawTraceEvents'
afterEach(cleanup)
it('labels nested events by their actual type and lets users filter the history', () => {
  render(<RawTraceEvents events={[
    { id: 1, type: 'task.accepted', payload: {}, createdAt: '2026-09-30T00:00:00Z' },
    { id: 2, type: 'agent.audit', payload: { type: 'model.trace', payload: { phase: 'request', chunkIndex: 2, chunkCount: 4 } }, createdAt: '2026-09-30T00:00:01Z' },
  ]} />)
  const root = screen.getByText('原始事件').closest('details')!
  root.open = true
  fireEvent(root, new Event('toggle'))
  expect(screen.getByText('model.trace')).toBeVisible()
  expect(screen.getByText('请求片段 3/4')).toBeVisible()
  expect(screen.queryByText('agent.audit')).not.toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('搜索原始事件'), { target: { value: 'model.trace' } })
  expect(screen.queryByText('task.accepted')).not.toBeInTheDocument()
  expect(screen.getByText('显示 1 / 2 条事件')).toBeInTheDocument()
})
