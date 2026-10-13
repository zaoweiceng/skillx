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
  expect(screen.queryByText('目录清单 · 2 项')).not.toBeInTheDocument()
  fireEvent.click(screen.getByText('列出 agents 目录，定位入口文件'))
  expect(screen.getByText('目录清单 · 2 项')).toBeTruthy()
  expect(screen.queryByText('print("secret source")')).toBeNull()
  fireEvent.click(screen.getByText('查看执行代码'))
  expect(screen.getByText('print("secret source")')).toBeTruthy()
})

it('displays independently extracted guides and executable capabilities', () => {
  render(<TaskRecords id="task" invocations={[]} learning={[{id: 'job', kind: 'distill', status: 'published', result: {
    skills: {'analysis-guide': 'guide-id'}, skillx: {'format-report': 'cap-id'}, reason: '只提取复用部分',
  }}]} />)
  fireEvent.click(screen.getByText('能力提炼与固化'))
  expect(screen.getByText('Skill：analysis-guide')).toBeInTheDocument()
  expect(screen.getByText('SkillX：format-report')).toBeInTheDocument()
  expect(screen.getByText('只提取复用部分')).toBeInTheDocument()
  expect(screen.queryByText('工作流固化')).not.toBeInTheDocument()
})

it('shows specialist progress, validation feedback and compact usage', () => {
  render(<TaskRecords id="task" invocations={[]} learning={[{id: 'job', kind: 'distill', status: 'queued', result: {
    agentStages: {
      extractor: {role: 'extractor', status: 'completed', totalTokens: 12500, output: {decision: 'candidate'}},
      'skillx:format': {role: 'skillx', name: 'format', status: 'needs_revision', validationStatus: 'failed', error: '不同输入验证失败', attempt: 2},
      'workflow:report': {role: 'workflow', name: 'report', status: 'running'},
    },
  }}]} />)
  fireEvent.click(screen.getByText('能力提炼与固化'))
  expect(screen.getByLabelText('后台专业 Agent 阶段')).toBeInTheDocument()
  expect(screen.getByText('片段提炼 Agent')).toBeInTheDocument()
  expect(screen.getByText('SkillX 构建 Agent')).toBeInTheDocument()
  expect(screen.getByText('工作流编排 Agent')).toBeInTheDocument()
  expect(screen.getByText('12.5K Token')).toBeInTheDocument()
  expect(screen.getByText('不同输入验证失败')).toBeInTheDocument()
  expect(screen.getByText('第 2 次尝试')).toBeInTheDocument()
  expect(screen.queryByText(/"decision"/)).not.toBeInTheDocument()
  fireEvent.click(screen.getByText('查看阶段产物'))
  expect(screen.getByText(/"decision": "candidate"/)).toBeInTheDocument()
})
