import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import ComponentsPage from '@/pages/ComponentsPage'
import SettingsPage from '@/pages/SettingsPage'
import { defaultWorkflow } from '@/lib/defaultWorkflow'

const listComponents = vi.fn()
const listWorkflows = vi.fn()
const getHealth = vi.fn()

vi.mock('@/api/http', () => ({ request: vi.fn().mockResolvedValue([]) }))

vi.mock('@/api/client', () => ({
  api: {
    listComponents: (...args: unknown[]) => listComponents(...args),
    listWorkflows: (...args: unknown[]) => listWorkflows(...args),
    getHealth: (...args: unknown[]) => getHealth(...args),
  },
}))

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('产品状态页面', () => {
  it('按发布状态筛选统一工作流列表并暴露 aria-pressed', async () => {
    listWorkflows.mockResolvedValue([
      { id: 'draft', name: '草稿工作流', definition: defaultWorkflow },
      { id: 'published', name: '已发布工作流', definition: defaultWorkflow },
    ])
    listComponents.mockResolvedValue([
      {
        id: 'agent',
        workflowId: 'published',
        name: 'Agent 组件',
        description: '',
        executionClass: 'agent',
        visibility: 'private',
      },
    ])
    render(
      <MemoryRouter>
        <ComponentsPage />
      </MemoryRouter>,
    )
    await waitFor(() => expect(screen.getByText('草稿工作流')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: '已发布' }))
    expect(screen.getByRole('button', { name: '已发布' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    expect(screen.getByText('已发布工作流')).toBeInTheDocument()
    expect(screen.queryByText('草稿工作流')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /^草\s*稿$/ }))
    expect(screen.getByRole('button', { name: /^草\s*稿$/ })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByText('草稿工作流')).toBeInTheDocument()
    expect(screen.queryByText('已发布工作流')).not.toBeInTheDocument()
  })

  it('fixture 仅显示协议事实，自动选择保持不可用', async () => {
    getHealth.mockResolvedValue({
      status: 'ready',
      migration: '0020',
      workflowSelectionMode: 'unavailable',
      selectionProvider: {
        type: 'fixture',
        protocolTestOnly: true,
        configured: true,
        protocolAvailable: true,
        modelAvailable: false,
        lastProbe: {
          at: '2026-08-10T06:00:00Z',
          ok: true,
          summary: '协议 fixture 可用，仅用于协议/故障测试',
        },
      },
      runtimes: {
        database: { status: 'available', detail: 'SQLite 可用' },
        worker: { status: 'unknown', detail: '未检测' },
        browser: {
          status: 'permission_denied',
          detail: 'Chromium 启动被操作系统权限拒绝',
          checkedAt: '2026-08-10T06:00:00Z',
          launchSucceeded: false,
        },
        secretProvider: { status: 'reference_only', detail: '引用模式' },
      },
    })
    render(<SettingsPage />)
    await waitFor(() => expect(screen.getByText('连接诊断与高级信息')).toBeInTheDocument())
    fireEvent.click(screen.getByText('连接诊断与高级信息'))
    await waitFor(() => expect(screen.getByText('fixture')).toBeInTheDocument())
    expect(screen.getByText(/自动选择不可用/)).toBeInTheDocument()
    expect(screen.getByText(/不参与产品语义选择/)).toBeInTheDocument()
    expect(screen.getByText(/协议 fixture 可用/)).toBeInTheDocument()
    expect(screen.getByText('permission_denied')).toBeInTheDocument()
  })
})
