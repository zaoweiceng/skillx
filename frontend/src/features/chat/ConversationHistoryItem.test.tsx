import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ConversationHistoryItem } from './ConversationHistoryItem'

afterEach(cleanup)

describe('conversation history actions', () => {
  it('trims and persists a renamed title', async () => {
    const rename = vi.fn().mockResolvedValue(undefined)
    render(<MemoryRouter><ConversationHistoryItem id="one" title="旧标题" active onNavigate={() => {}} onRename={rename} onDelete={vi.fn()} /></MemoryRouter>)
    fireEvent.click(screen.getByRole('button', { name: '管理对话：旧标题' }))
    fireEvent.click(await screen.findByText('重命名'))
    fireEvent.change(screen.getByRole('textbox', { name: '对话标题' }), { target: { value: ' 新标题 ' } })
    fireEvent.click(screen.getByRole('button', { name: '保 存' }))
    await waitFor(() => expect(rename).toHaveBeenCalledWith('one', '新标题'))
  })
  it('keeps failures visible and allows retrying deletion', async () => {
    const remove = vi.fn().mockRejectedValueOnce(new Error('网络失败')).mockResolvedValue(undefined)
    render(<MemoryRouter><ConversationHistoryItem id="two" title="测试" active={false} onNavigate={() => {}} onRename={vi.fn()} onDelete={remove} /></MemoryRouter>)
    fireEvent.click(screen.getByRole('button', { name: '管理对话：测试' }))
    fireEvent.click(await screen.findByText('删除'))
    expect(remove).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '删 除' }))
    expect(await screen.findByText('网络失败')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '删 除' }))
    await waitFor(() => expect(remove).toHaveBeenCalledTimes(2))
  })
})
