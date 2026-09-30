import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { TaskTrace } from './TaskTrace'
import { request } from '@/api/http'
vi.mock('@/api/http', () => ({ request: vi.fn() }))
afterEach(() => { cleanup(); vi.useRealTimers(); vi.resetAllMocks() })
it('recovers after a failed fetch, advances the cursor and stops when closed', async () => {
  vi.useFakeTimers()
  vi.mocked(request).mockRejectedValueOnce(new Error('暂时离线')).mockResolvedValueOnce([
    { id: 17, type: 'task.succeeded', payload: {}, createdAt: '2026-09-30T03:00:00Z' },
  ]).mockResolvedValue([])
  const view = render(<TaskTrace id="test-task" />)
  await act(async () => {})
  expect(screen.getByText('暂时离线')).toBeInTheDocument()
  await act(async () => { await vi.advanceTimersByTimeAsync(1500) })
  expect(screen.getByText('任务完成')).toBeInTheDocument()
  expect(screen.queryByText('暂时离线')).not.toBeInTheDocument()
  await act(async () => { await vi.advanceTimersByTimeAsync(1500) })
  expect(request).toHaveBeenLastCalledWith('/tasks/test-task/events?after=17')
  view.unmount()
  await act(async () => { await vi.advanceTimersByTimeAsync(3000) })
  expect(request).toHaveBeenCalledTimes(3)
})
