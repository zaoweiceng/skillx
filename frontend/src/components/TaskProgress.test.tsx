import { act, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { request } from '@/api/http'
import TaskProgress from './TaskProgress'
import { progressLabel, progressDetail } from './taskProgressEvents'
vi.mock('@/api/http', () => ({ request: vi.fn() }))
afterEach(() => { vi.useRealTimers(); vi.resetAllMocks() })
it('shows live model progress, advances the cursor, and stops when unmounted', async () => {
  vi.useFakeTimers()
  vi.mocked(request).mockResolvedValueOnce([{id: 7, type: 'agent.audit', payload: {type: 'model.request.dispatched'}, createdAt: new Date().toISOString()}])
    .mockResolvedValueOnce([{id: 8, type: 'task.decision', payload: {summary: '已读取目录，准备分析文件'}, createdAt: new Date().toISOString()}])
  const view = render(<TaskProgress id="task" finished={false} invocations={[]} />)
  await act(async () => {})
  expect(screen.getByRole('status')).toHaveTextContent('等待模型响应')
  await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
  expect(request).toHaveBeenLastCalledWith('/tasks/task/events?after=7', expect.any(Object))
  expect(screen.getByRole('status')).toHaveTextContent('已读取目录，准备分析文件')
  view.unmount()
  await act(async () => { await vi.advanceTimersByTimeAsync(2000) })
  expect(request).toHaveBeenCalledTimes(2)
})
it('does not display raw reasoning events', () => {
  expect(progressLabel({id: 1, type: 'agent.audit', payload: {type: 'reasoning.delta', text: 'private'}, createdAt: ''})).toBeNull()
})

it('explains concrete code operations and distinguishes model waits', () => {
  const base = {id: 1, createdAt: ''}
  expect(progressDetail({...base, type: 'invocation.queued', payload: {kind: 'code', reason: '列出 agents 目录', request: {language: 'python', source: "print('ok')"}}})).toContain('列出 agents 目录')
  expect(progressDetail({...base, type: 'invocation.succeeded', payload: {output: {stdout: 'ok'}}})).toContain('已返回 2 字符输出')
  expect(progressDetail({...base, type: 'agent.audit', payload: {type: 'model.request.dispatched'}})).toContain('不代表已经读取文件或执行命令')
})
