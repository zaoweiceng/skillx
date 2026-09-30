import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { DirectoryPicker } from './DirectoryPicker'

vi.mock('@/api/client', () => ({ api: { workspaceDirectories: vi.fn() } }))

describe('DirectoryPicker', () => {
  it('only submits a directory validated by the server', async () => {
    vi.mocked(api.workspaceDirectories).mockResolvedValue({
      path: '/workspace', parent: '/', directories: [{ name: 'project', path: '/workspace/project' }],
    })
    const onChange = vi.fn()
    render(<DirectoryPicker onChange={onChange} disabled={false} />)
    fireEvent.click(screen.getByRole('button', { name: '选择工作目录' }))
    await screen.findByRole('button', { name: 'project' })
    fireEvent.change(screen.getByRole('searchbox', { name: '目录路径' }), { target: { value: '/missing' } })
    expect(screen.getByRole('button', { name: '使用此目录' })).toBeDisabled()
    vi.mocked(api.workspaceDirectories).mockRejectedValueOnce(new Error('目录不存在'))
    fireEvent.click(screen.getByRole('button', { name: /打\s*开/ }))
    await screen.findByText('目录不存在')
    expect(screen.getByRole('button', { name: '使用此目录' })).toBeDisabled()
    vi.mocked(api.workspaceDirectories).mockResolvedValueOnce({ path: '/workspace/project', parent: '/workspace', directories: [] })
    fireEvent.change(screen.getByRole('searchbox', { name: '目录路径' }), { target: { value: '/workspace/project' } })
    fireEvent.click(screen.getByRole('button', { name: /打\s*开/ }))
    await waitFor(() => expect(screen.getByRole('button', { name: '使用此目录' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: '使用此目录' }))
    expect(onChange).toHaveBeenCalledWith('/workspace/project')
  })
})
