import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import MonitoringPage from './MonitoringPage'
import { monitoring } from '@/api/monitoring'

vi.mock('@/api/monitoring', () => ({ monitoring: { metric: vi.fn(), sessions: vi.fn(), records: vi.fn() } }))
afterEach(() => { cleanup(); vi.resetAllMocks() })

it('filters cards independently, refreshes the live window, and opens original records', async () => {
  vi.mocked(monitoring.metric).mockResolvedValue({ value: null, basis: '统计口径', items: [] })
  vi.mocked(monitoring.sessions).mockResolvedValue({ items: [{ id: 'session', runId: 'task-run', cwd: '/workspace', createdAt: new Date().toISOString(), bytes: 1024 }], total: 1 })
  vi.mocked(monitoring.records).mockResolvedValue({ items: [{ timestamp: new Date().toISOString(), type: 'session_meta', payload: { runId: 'task-run' } }], nextOffset: null })
  render(<MemoryRouter><MonitoringPage /></MemoryRouter>)
  await screen.findByText('task-run')
  const tokens = screen.getByText('Token 消耗').closest('.ant-card')! as HTMLElement
  vi.mocked(monitoring.metric).mockClear()
  fireEvent.click(within(tokens).getByText('7天'))
  await waitFor(() => expect(monitoring.metric).toHaveBeenCalled())
  expect(vi.mocked(monitoring.metric).mock.calls.every(([key]) => key === 'tokens')).toBe(true)
  const [, range] = vi.mocked(monitoring.metric).mock.calls.at(-1)!
  expect(+new Date(range.end) - +new Date(range.start)).toBe(7 * 86400000)
  fireEvent.click(screen.getByText('查看原始记录'))
  expect(await screen.findByText(/session_meta/)).toBeInTheDocument()
  expect(monitoring.records).toHaveBeenCalledWith('session', 0, expect.any(AbortSignal))
})

it('offers shorter Token ranges and renders five metrics as time series', async () => {
  vi.mocked(monitoring.metric).mockResolvedValue({ value: 6369137, basis: '统计口径', intervalSeconds: 300, series: [
    { start: '2026-10-13T01:00:00Z', end: '2026-10-13T01:05:00Z', value: 6369137, samples: 1 },
  ] })
  vi.mocked(monitoring.sessions).mockResolvedValue({ items: [], total: 0 })
  render(<MemoryRouter><MonitoringPage /></MemoryRouter>)
  await screen.findByRole('img', { name: /Token 消耗趋势/ })
  expect(screen.getAllByRole('img', { name: /趋势，每/ })).toHaveLength(5)
  expect(screen.getAllByText('6.37M').length).toBeGreaterThan(0)
  fireEvent.click(screen.getByText('1小时'))
  await waitFor(() => {
    const [, range] = vi.mocked(monitoring.metric).mock.calls.at(-1)!
    expect(+new Date(range.end) - +new Date(range.start)).toBe(3600000)
  })
  expect(screen.getByText('3小时')).toBeInTheDocument()
  expect(screen.getByText('12小时')).toBeInTheDocument()
})

it('places the duration ranking below trends and links each task to its conversation', async () => {
  vi.mocked(monitoring.metric).mockResolvedValue({ value: 90000, basis: '统计口径', ranking: [
    { id: 'task-1', goal: '慢任务', status: 'succeeded', conversationId: 'conversation-1', createdAt: '2026-10-13T01:00:00Z', durationMs: 120000 },
    { id: 'task-2', goal: '快任务', status: 'failed', conversationId: null, createdAt: '2026-10-13T02:00:00Z', durationMs: 60000 },
  ] })
  vi.mocked(monitoring.sessions).mockResolvedValue({ items: [], total: 0 })
  const { container } = render(<MemoryRouter><MonitoringPage /></MemoryRouter>)
  expect(await screen.findByRole('link', { name: '慢任务' })).toHaveAttribute('href', '/chat/conversation-1')
  const card = screen.getByText('任务耗时排行').closest('.ant-card')! as HTMLElement
  expect(within(card).getByText('2.0 分钟')).toBeInTheDocument()
  expect(within(card).getByText('成功')).toBeInTheDocument()
  expect(within(card).queryByRole('img', { name: /趋势/ })).not.toBeInTheDocument()
  const titles = Array.from(container.querySelectorAll('.ant-card-head-title')).map(node => node.textContent)
  expect(titles.indexOf('任务耗时排行')).toBeGreaterThan(titles.indexOf('Workflow 调用量'))
  expect(titles.indexOf('任务耗时排行')).toBeLessThan(titles.indexOf('失败原因排行'))
})
