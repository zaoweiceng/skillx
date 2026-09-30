import { App, ConfigProvider } from 'antd'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import SkillsPage from './SkillsPage'
import { skillsApi } from '@/api/skills'

vi.mock('@/api/skills', () => ({skillsApi: {list: vi.fn(), get: vi.fn(), create: vi.fn(), update: vi.fn(), import: vi.fn(), remove: vi.fn(), convert: vi.fn()}}))
vi.mock('@/features/chat/DirectoryPicker', () => ({DirectoryPicker: () => null}))
const skill = {id: 'one', name: 'report', displayName: '报告助手', description: '整理报告', enabled: true, revision: 2,
  mode: 'skill' as const, skillxVersionId: null, skillxVersion: null, interfaceKind: null, updatedAt: ''}
const detail = {...skill, content: '原始指令', resources: ['SKILL.md'], conversion: {
  kind: 'agent' as const, inputSchema: {type: 'object'}, outputSchema: {type: 'object'}, source: '', examples: [], hasManifest: false}}
beforeEach(() => {vi.mocked(skillsApi.list).mockResolvedValue([skill]); vi.mocked(skillsApi.get).mockResolvedValue(detail)})
afterEach(() => {cleanup(); vi.clearAllMocks()})
const show = () => render(<ConfigProvider button={{autoInsertSpace: false}}><App><SkillsPage /></App></ConfigProvider>)

it('creates a manually added Skill and retains validation errors in the editor', async () => {
  show()
  fireEvent.click(screen.getByRole('button', {name: '添加 Skill'}))
  fireEvent.change(screen.getByLabelText('Skill 标识'), {target: {value: 'new-skill'}})
  fireEvent.change(screen.getByLabelText('Skill 名称'), {target: {value: '新技能'}})
  fireEvent.change(screen.getByLabelText('Skill 用途'), {target: {value: '整理数据'}})
  fireEvent.change(screen.getByLabelText('Skill 指令'), {target: {value: '输出 JSON'}})
  vi.mocked(skillsApi.create).mockRejectedValueOnce(new Error('指令无效'))
  fireEvent.click(screen.getByRole('button', {name: '保存'}))
  expect(await screen.findByText('指令无效')).toBeInTheDocument()
  expect(screen.getByLabelText('Skill 指令')).toHaveValue('输出 JSON')
  vi.mocked(skillsApi.create).mockResolvedValue(detail)
  fireEvent.click(screen.getByRole('button', {name: '保存'}))
  await waitFor(() => expect(skillsApi.create).toHaveBeenLastCalledWith({name: 'new-skill', displayName: '新技能', description: '整理数据', content: '输出 JSON', enabled: true}))
  await waitFor(() => expect(screen.queryByLabelText('Skill 指令')).not.toBeInTheDocument())
})

it('submits edited contracts with the source revision and refreshes the converted card', async () => {
  show()
  fireEvent.click(await screen.findByRole('button', {name: '转为 SkillX'}))
  await screen.findByLabelText('输入契约')
  fireEvent.change(screen.getByLabelText('输入契约'), {target: {value: '{"type":"object","required":["task"]}'}})
  vi.mocked(skillsApi.convert).mockResolvedValue({...detail, mode: 'skillx'})
  vi.mocked(skillsApi.list).mockResolvedValue([{...skill, mode: 'skillx', interfaceKind: 'agent', skillxVersion: '1.0.0'}])
  fireEvent.click(screen.getByRole('button', {name: '校验契约并转换'}))
  await waitFor(() => expect(skillsApi.convert).toHaveBeenCalledWith('one', {
    kind: 'agent', expectedRevision: 2, inputSchema: {type: 'object', required: ['task']}, outputSchema: {type: 'object'}, source: '', examples: [],
  }))
  expect(await screen.findByText('SkillX · Agent')).toBeInTheDocument()
})

it('filters cards and disables a Skill while retaining its contents', async () => {
  vi.mocked(skillsApi.list).mockResolvedValue([skill, {...skill, id: 'two', displayName: '脚本助手', mode: 'skillx', interfaceKind: 'direct'}])
  show()
  await screen.findByText('报告助手')
  fireEvent.click(screen.getByRole('button', {name: 'SkillX'}))
  expect(screen.queryByText('报告助手')).not.toBeInTheDocument()
  expect(screen.getByText('脚本助手')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', {name: '全部'}))
  vi.mocked(skillsApi.update).mockResolvedValue({...detail, enabled: false})
  fireEvent.click(screen.getByRole('switch', {name: '启用 报告助手'}))
  await waitFor(() => expect(skillsApi.update).toHaveBeenCalledWith('one', {name: 'report', displayName: '报告助手', description: '整理报告', content: '原始指令', enabled: false}))
})
