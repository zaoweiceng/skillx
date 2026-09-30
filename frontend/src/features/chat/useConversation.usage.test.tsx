import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'
import { useConversation } from './useConversation'
import type { ConversationTurn } from '@/api/client'

const getConversation = vi.fn()
const sendMessage = vi.fn()
vi.mock('@/api/client', () => ({ api: {
  getConversation: (...args: unknown[]) => getConversation(...args),
  sendMessage: (...args: unknown[]) => sendMessage(...args),
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
function SendImageProbe() {
  const { send } = useConversation()
  return <form onSubmit={event => void send(event, [], undefined, [{ name: 'image.png', mimeType: 'image/png', data: 'image-data' }])}>
    <button type="submit">发送图片</button>
  </form>
}
const snapshot = (tokens: number | null, completed = false) => ({ id: 'conversation',
  messages: [{ id: 'message', turnId: 'turn', role: 'assistant', content: { type: 'task', taskId: 'task' } }],
  turns: [{ ...turn, completedAt: completed ? new Date().toISOString() : null,
    usage: { state: completed ? 'complete' : 'pending', totalTokens: tokens, stages: [] } }],
})
afterEach(() => { cleanup(); vi.useRealTimers(); vi.resetAllMocks() })
it('submits image-only messages through the API with a separate image payload', async () => {
  getConversation.mockResolvedValue({ id: 'conversation', messages: [], turns: [] })
  sendMessage.mockResolvedValue({ assistant: { type: 'task', taskId: 'task' } })
  render(<MemoryRouter initialEntries={['/chat/conversation']}><Routes><Route path="/chat/:id" element={<SendImageProbe />} /></Routes></MemoryRouter>)
  await act(async () => {})
  fireEvent.click(screen.getByRole('button', { name: '发送图片' }))
  await waitFor(() => expect(sendMessage).toHaveBeenCalledWith('conversation', '请分析这些图片。', [], undefined,
    [{ name: 'image.png', mimeType: 'image/png', data: 'image-data' }]))
})
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
