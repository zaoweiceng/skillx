import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import ComponentsPage from '@/pages/ComponentsPage'
import SettingsPage from '@/pages/SettingsPage'

const listComponents = vi.fn()
const getHealth = vi.fn()

vi.mock('@/api/client', () => ({
  api: {
    listComponents: (...args: unknown[]) => listComponents(...args),
    getHealth: (...args: unknown[]) => getHealth(...args),
  },
}))

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('产品状态页面', () => {
  it('按 executionClass 筛选组件并暴露 aria-pressed', async () => {
    listComponents.mockResolvedValue([
      {
        id: 'zero',
        name: '零模型组件',
        description: '',
        executionClass: 'zero_llm',
        visibility: 'private',
      },
      {
        id: 'agent',
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
    await waitFor(() => expect(screen.getByText('零模型组件')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: 'Agent 工作流' }))
    expect(screen.getByRole('button', { name: 'Agent 工作流' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    expect(screen.getByText('Agent 组件')).toBeInTheDocument()
    expect(screen.queryByText('零模型组件')).not.toBeInTheDocument()
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
    await waitFor(() => expect(screen.getByText('fixture')).toBeInTheDocument())
    expect(screen.getByText(/自动选择不可用/)).toBeInTheDocument()
    expect(screen.getByText(/不参与产品语义选择/)).toBeInTheDocument()
    expect(screen.getByText(/协议 fixture 可用/)).toBeInTheDocument()
    expect(screen.getByText('permission_denied')).toBeInTheDocument()
  })
})
