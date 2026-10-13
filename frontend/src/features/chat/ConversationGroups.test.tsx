import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, expect, it, vi } from 'vitest'
import { ConversationGroups } from './ConversationGroups'

const { list, add } = vi.hoisted(() => ({ list: vi.fn(), add: vi.fn() }))
vi.mock('@/api/client', () => ({ api: {
  listChatWorkspaces: async () => ({ items: ['/code/app'] }),
  listConversations: list,
  addChatWorkspace: add,
} }))
vi.mock('./DirectoryPicker', () => ({ DirectoryPicker: ({ onChange }: { onChange: (path: string) => void }) =>
  <button onClick={() => onChange('/code/new')}>添加工作区</button> }))
const props = { history: [], conversationId: '', onNavigate: vi.fn(), onNew: vi.fn(), onRename: vi.fn(), onDelete: vi.fn() }
beforeEach(() => {
  localStorage.clear()
  vi.clearAllMocks()
  list.mockImplementation(async (_cursor, _query, directory) => ({ items: [{ id: directory || 'normal', title: directory ? '项目对话' : '普通聊天' }], nextCursor: null }))
  add.mockResolvedValue({ path: '/code/new' })
})
it('groups conversations, remembers collapse and inherits the selected directory', async () => {
  const view = render(<MemoryRouter><ConversationGroups {...props} /></MemoryRouter>)
  await screen.findByText('项目对话')
  expect(list).toHaveBeenCalledWith(undefined, undefined, '')
  expect(list).toHaveBeenCalledWith(undefined, undefined, '/code/app')
  fireEvent.click(screen.getByRole('button', { name: '在app中新建对话' }))
  expect(props.onNew).toHaveBeenLastCalledWith('/code/app')
  fireEvent.click(screen.getByRole('button', { name: '在对话中新建对话' }))
  expect(props.onNew).toHaveBeenLastCalledWith(undefined)
  fireEvent.click(screen.getByRole('button', { name: 'app' }))
  expect(screen.queryByText('项目对话')).not.toBeInTheDocument()
  view.unmount()
  render(<MemoryRouter><ConversationGroups {...props} /></MemoryRouter>)
  expect(await screen.findByRole('button', { name: 'app' })).toHaveAttribute('aria-expanded', 'false')
  fireEvent.click(screen.getByRole('button', { name: '添加工作区' }))
  await waitFor(() => expect(props.onNew).toHaveBeenLastCalledWith('/code/new'))
  expect(add).toHaveBeenCalledWith('/code/new')
})
