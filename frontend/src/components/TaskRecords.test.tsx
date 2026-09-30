import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import TaskRecords from './TaskRecords'
import { request } from '@/api/http'
vi.mock('@/api/http', () => ({ request: vi.fn() }))
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })
it('downloads execution evidence as a named JSON file', async () => {
  vi.mocked(request).mockResolvedValue({schemaVersion: '1', events: []})
  const createObjectURL = vi.fn((blob: Blob) => { expect(blob.type).toBe('application/json'); return 'blob:task-record' })
  vi.stubGlobal('URL', {createObjectURL, revokeObjectURL: vi.fn()})
  let filename = ''
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function(this: HTMLAnchorElement) { filename = this.download })
  render(<TaskRecords id="task-123" invocations={[]} learning={[]} />)
  fireEvent.click(screen.getByRole('button', {name: '下载执行记录'}))
  await waitFor(() => expect(filename).toBe('skillx-task-task-123.json'))
  expect(request).toHaveBeenCalledWith('/tasks/task-123/session-export')
  expect(createObjectURL).toHaveBeenCalledWith(expect.objectContaining({type: 'application/json'}))
})

it('shows purpose and compact results while keeping source and full output available', () => {
  render(<TaskRecords id="t" learning={[]} invocations={[{id: 'call', kind: 'code', status: 'succeeded', reason: '列出 agents 目录，定位入口文件', workingDirectory: '/project', request: {language: 'python', source: 'print("secret source")'}, output: {exitCode: 0, stdout: JSON.stringify({'/project/agents': ['F\ta.md', 'D\tskills']})}, error: null}]} />)
  fireEvent.click(screen.getByText('调用记录'))
  expect(screen.getByText('列出 agents 目录，定位入口文件')).toBeTruthy()
  expect(screen.getByText('目录清单 · 2 项')).toBeTruthy()
  expect(screen.queryByText('print("secret source")')).toBeNull()
  fireEvent.click(screen.getByText('查看执行代码'))
  expect(screen.getByText('print("secret source")')).toBeTruthy()
})
