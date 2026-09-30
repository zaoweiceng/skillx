import { act, cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'
import { useConversation } from './useConversation'
import type { ConversationTurn } from '@/api/client'

const getConversation = vi.fn()
vi.mock('@/api/client', () => ({ api: {
  getConversation: (...args: unknown[]) => getConversation(...args),
  getHealth: async () => ({ workflowSelectionMode: 'real' }),
  listConversations: async () => ({ items: [], nextCursor: null }),
} }))
const turn: ConversationTurn = { id: 'turn', status: 'processing', workflowModeSnapshot: 'enabled',
  acceptedAt: new Date().toISOString(), firstResponseAt: null, completedAt: null, elapsedMs: null,
  usage: { state: 'pending', totalTokens: null, stages: [] } }
function Probe() {
  const { tokenLabel, turnFor, messages } = useConversation()
  return <p>{tokenLabel(messages[0] ? turnFor(messages[0]) : undefined)}</p>
}
const snapshot = (tokens: number | null, completed = false) => ({ id: 'conversation',
  messages: [{ id: 'message', turnId: 'turn', role: 'assistant', content: { type: 'task', taskId: 'task' } }],
  turns: [{ ...turn, completedAt: completed ? new Date().toISOString() : null,
    usage: { state: completed ? 'complete' : 'pending', totalTokens: tokens, stages: [] } }],
})
afterEach(() => { cleanup(); vi.useRealTimers(); vi.resetAllMocks() })
it('updates known token counts during execution and stops polling after completion', async () => {
  vi.useFakeTimers()
  getConversation.mockResolvedValueOnce(snapshot(null)).mockResolvedValueOnce(snapshot(1234)).mockResolvedValue(snapshot(2345, true))
  render(<MemoryRouter initialEntries={['/chat/conversation']}><Routes><Route path="/chat/:id" element={<Probe />} /></Routes></MemoryRouter>)
  await act(async () => {})
  expect(screen.getByText('Token 等待模型上报')).toBeInTheDocument()
  await act(async () => { await vi.advanceTimersByTimeAsync(1500) })
  expect(screen.getByText('已用 1.2K Token · 更新中')).toBeInTheDocument()
  await act(async () => { await vi.advanceTimersByTimeAsync(1500) })
  expect(screen.getByText('2.3K Token')).toBeInTheDocument()
  const calls = getConversation.mock.calls.length
  await act(async () => { await vi.advanceTimersByTimeAsync(3000) })
  expect(getConversation).toHaveBeenCalledTimes(calls)
})
