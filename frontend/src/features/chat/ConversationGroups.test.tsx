import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ConversationGroups } from './ConversationGroups'

const { list, add, update, remove } = vi.hoisted(() => ({ list: vi.fn(), add: vi.fn(), update: vi.fn(), remove: vi.fn() }))
vi.mock('@/api/client', () => ({ api: {
  listChatWorkspaces: async () => ({ items: [{ path: '/code/app', name: 'app' }] }),
  listConversations: list,
  addChatWorkspace: add,
  updateChatWorkspace: update,
  deleteChatWorkspace: remove,
} }))
vi.mock('./DirectoryPicker', () => ({ DirectoryPicker: ({ onChange }: { onChange: (path: string) => void }) =>
  <button onClick={() => onChange('/code/new')}>添加工作区</button> }))
const props = { history: [], conversationId: '', onNavigate: vi.fn(), onNew: vi.fn(), onRename: vi.fn(), onDelete: vi.fn() }
afterEach(() => cleanup())
beforeEach(() => {
  localStorage.clear()
  vi.clearAllMocks()
  list.mockImplementation(async (_cursor, _query, directory) => ({ items: [{ id: directory || 'normal', title: directory ? '项目对话' : '普通聊天' }], nextCursor: null }))
  add.mockResolvedValue({ path: '/code/new', name: 'new' })
})
it('groups conversations, remembers collapse and inherits the selected directory', async () => {
  const view = render(<MemoryRouter><ConversationGroups {...props} /></MemoryRouter>)
  await screen.findByText('项目对话')
  expect(list).toHaveBeenCalledWith(undefined, undefined, '')
  expect(list).toHaveBeenCalledWith(undefined, undefined, '/code/app')
  fireEvent.click(screen.getByRole('button', { name: '在app中新建对话' }))
  expect(props.onNew).toHaveBeenLastCalledWith('/code/app', '/code/app')
  fireEvent.click(screen.getByRole('button', { name: '在对话中新建对话' }))
  expect(props.onNew).toHaveBeenLastCalledWith(undefined, undefined)
  fireEvent.click(screen.getByRole('button', { name: 'app' }))
  expect(screen.queryByText('项目对话')).not.toBeInTheDocument()
  view.unmount()
  render(<MemoryRouter><ConversationGroups {...props} /></MemoryRouter>)
  expect(await screen.findByRole('button', { name: 'app' })).toHaveAttribute('aria-expanded', 'false')
  fireEvent.click(screen.getByRole('button', { name: '添加工作区' }))
  await waitFor(() => expect(props.onNew).toHaveBeenLastCalledWith('/code/new', '/code/new'))
  expect(add).toHaveBeenCalledWith('/code/new')
})


it('renames a workspace through its compact menu', async () => {
  update.mockResolvedValue({ path: '/code/app', name: '新名称' })
  render(<MemoryRouter><ConversationGroups {...props} /></MemoryRouter>)
  fireEvent.click(await screen.findByRole('button', { name: '管理工作区：app' }))
  fireEvent.click(await screen.findByText('重命名'))
  fireEvent.change(screen.getByLabelText('工作区名称'), { target: { value: '新名称' } })
  fireEvent.click(screen.getByRole('button', { name: '保 存' }))
  await waitFor(() => expect(update).toHaveBeenCalledWith('/code/app', { name: '新名称' }))
  expect(await screen.findByRole('button', { name: '管理工作区：新名称' })).toBeInTheDocument()
})

it.each([false, true])('deletes a workspace with explicit conversation choice: %s', async removeChats => {
  remove.mockResolvedValue({ deleted: true, conversationIds: ['chat-1'] })
  const changed = vi.fn()
  render(<MemoryRouter><ConversationGroups {...props} onWorkspaceChanged={changed} /></MemoryRouter>)
  fireEvent.click(await screen.findByRole('button', { name: '管理工作区：app' }))
  fireEvent.click(await screen.findByRole('menuitem', { name: '删除工作区' }))
  expect(screen.getByRole('radio', { name: '移到普通对话' })).toBeChecked()
  if (removeChats) fireEvent.click(screen.getByRole('radio', { name: '一起删除对话' }))
  fireEvent.click(screen.getByRole('button', { name: '删除工作区' }))
  await waitFor(() => expect(remove).toHaveBeenCalledWith('/code/app', removeChats))
  await waitFor(() => expect(changed).toHaveBeenCalledWith('/code/app', undefined, removeChats ? ['chat-1'] : []))
  expect(screen.queryByRole('button', { name: '管理工作区：app' })).not.toBeInTheDocument()
})


it('updates the directory and keeps the display name', async () => {
  update.mockResolvedValue({ path: '/code/next', name: 'app' })
  const changed = vi.fn()
  render(<MemoryRouter><ConversationGroups {...props} onWorkspaceChanged={changed} /></MemoryRouter>)
  fireEvent.click(await screen.findByRole('button', { name: '管理工作区：app' }))
  fireEvent.click(await screen.findByText('修改目录'))
  fireEvent.change(screen.getByLabelText('工作区目录'), { target: { value: '/code/next' } })
  fireEvent.click(screen.getByRole('button', { name: '保 存' }))
  await waitFor(() => expect(update).toHaveBeenCalledWith('/code/app', { workingDirectory: '/code/next' }))
  await waitFor(() => expect(changed).toHaveBeenCalledWith('/code/app', '/code/next', []))
  await waitFor(() => expect(list).toHaveBeenCalledWith(undefined, undefined, '/code/next'))
})
