import { act, fireEvent, render, screen, within } from '@testing-library/react'
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

it('shows one live timeline with newest stages first and prepends polled progress', async () => {
  vi.useFakeTimers()
  const event = (id: number) => ({ id, type: 'task.decision', payload: { summary: `阶段 ${id}` }, createdAt: '2026-10-12T06:40:00Z' })
  vi.mocked(request).mockResolvedValueOnce([1, 2, 3, 4, 5].map(event))
    .mockResolvedValueOnce([event(6)])
  const view = render(<TaskProgress id="task" finished={false} invocations={[]} />)
  await act(async () => {})
  const labels = (list: HTMLElement) => within(list).getAllByRole('listitem').map(item => item.textContent?.match(/阶段 \d+/)?.[0])
  expect(labels(screen.getByRole('list'))).toEqual(['阶段 5', '阶段 4', '阶段 3', '阶段 2'])
  expect(screen.queryByText('查看完整执行过程')).not.toBeInTheDocument()
  await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
  expect(request).toHaveBeenLastCalledWith('/tasks/task/events?after=5', expect.any(Object))
  expect(labels(screen.getByRole('list'))).toEqual(['阶段 6', '阶段 5', '阶段 4', '阶段 3'])
  view.unmount()
})

it('keeps fetching specialist progress after the foreground task completes', async () => {
  vi.useFakeTimers()
  vi.mocked(request).mockResolvedValue([{id: 1, type: 'learning.agent.started', payload: {role: 'extractor'}, createdAt: new Date().toISOString()}])
  const view = render(<TaskProgress id="task" finished learningActive invocations={[]} />)
  await act(async () => {})
  await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
  expect(request).toHaveBeenCalledTimes(2)
  view.rerender(<TaskProgress id="task" finished learningActive={false} invocations={[]} />)
  await act(async () => {})
  const count = vi.mocked(request).mock.calls.length
  await act(async () => { await vi.advanceTimersByTimeAsync(2000) })
  expect(request).toHaveBeenCalledTimes(count)
  view.unmount()
})

it('explains concrete code operations and distinguishes model waits', () => {
  const base = {id: 1, createdAt: ''}
  expect(progressDetail({...base, type: 'invocation.queued', payload: {kind: 'code', reason: '列出 agents 目录', request: {language: 'python', source: "print('ok')"}}})).toContain('列出 agents 目录')
  expect(progressDetail({...base, type: 'invocation.succeeded', payload: {output: {stdout: 'ok'}}})).toContain('已返回 2 字符输出')
  expect(progressDetail({...base, type: 'agent.audit', payload: {type: 'model.request.dispatched'}})).toContain('不代表已经读取文件或执行命令')
})

it('任务结束时将过程与决策卡片一起折叠，仍可手动查看', async () => {
  vi.mocked(request).mockResolvedValue([{id: 1, type: 'task.decision', payload: {summary: '读取信息'}, createdAt: new Date().toISOString()}])
  const view = render(<TaskProgress id="fold-task" finished={false} invocations={[]}><p>我的决策记录</p></TaskProgress>)
  await act(async () => {})
  expect(screen.getByText('我的决策记录')).toBeVisible()
  view.rerender(<TaskProgress id="fold-task" finished invocations={[]}><p>我的决策记录</p></TaskProgress>)
  await act(async () => {})
  expect(screen.queryByText('我的决策记录')).not.toBeInTheDocument()
  fireEvent.click(screen.getByText('执行过程 · 1 个阶段'))
  expect(screen.getByText('我的决策记录')).toBeVisible()
  view.unmount()
})
