import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { WorkflowDefinition } from '@/api/client'
import WorkflowEditorPage from '@/pages/WorkflowEditorPage'

// React Flow 在 jsdom 下无法完整渲染，替换为轻量占位，聚焦结构化编辑逻辑。
vi.mock('@/components/EditableCanvas', () => ({
  EditableCanvas: ({ definition }: { definition: WorkflowDefinition }) => (
    <div data-testid="canvas">节点数 {definition.spec.nodes.length}</div>
  ),
}))
vi.mock('@/components/WorkflowGraph', () => ({
  WorkflowGraph: () => <div data-testid="graph" />,
}))

const getWorkflow = vi.fn()
const updateDraft = vi.fn()
const validateWorkflow = vi.fn()
const publishWorkflow = vi.fn()
const createComponent = vi.fn()

vi.mock('@/api/client', () => ({
  api: {
    getWorkflow: (...args: unknown[]) => getWorkflow(...args),
    updateDraft: (...args: unknown[]) => updateDraft(...args),
    validateWorkflow: (...args: unknown[]) => validateWorkflow(...args),
    publishWorkflow: (...args: unknown[]) => publishWorkflow(...args),
    createComponent: (...args: unknown[]) => createComponent(...args),
  },
}))

const definition: WorkflowDefinition = {
  apiVersion: 'workflow.platform/v1',
  kind: 'Workflow',
  metadata: { name: 'greeting' },
  spec: {
    inputSchema: { type: 'object', properties: { name: { type: 'string' } } },
    outputSchema: { type: 'object', properties: { message: { type: 'string' } } },
    nodes: [
      { id: 'input', type: 'input', displayName: '输入' },
      { id: 'output', type: 'output', displayName: '输出' },
    ],
    edges: [],
  },
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/workflows/wf-1/edit']}>
      <Routes>
        <Route path="/workflows/:id/edit" element={<WorkflowEditorPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('WorkflowEditorPage 可视化编辑', () => {
  beforeEach(() => {
    getWorkflow.mockReset()
    updateDraft.mockReset()
    validateWorkflow.mockReset()
    publishWorkflow.mockReset()
    createComponent.mockReset()
    getWorkflow.mockResolvedValue({
      id: 'wf-1',
      name: 'greeting',
      draftRevision: 1,
      definition,
      createdAt: '',
      updatedAt: '',
    })
  })

  afterEach(() => cleanup())

  it('通过调色板新增节点并在画布反映（无需编辑 JSON）', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByTestId('canvas')).toHaveTextContent('节点数 2'))
    // 点击调色板的 transform 节点。
    fireEvent.click(screen.getByRole('button', { name: /transform/ }))
    await waitFor(() => expect(screen.getByTestId('canvas')).toHaveTextContent('节点数 3'))
  })

  it('发布走 save→validate→publish→createComponent 全流程', async () => {
    updateDraft.mockResolvedValue({
      id: 'wf-1',
      name: 'greeting',
      draftRevision: 2,
      definition,
      createdAt: '',
      updatedAt: '',
    })
    validateWorkflow.mockResolvedValue({ valid: true, digest: 'sha256:x', diagnostics: [] })
    publishWorkflow.mockResolvedValue({ id: 'ver-1', version: 1, digest: 'sha256:x' })
    createComponent.mockResolvedValue({ id: 'comp-1234567', name: 'greeting' })

    renderPage()
    await waitFor(() => expect(screen.getByTestId('canvas')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /发布版本/ }))
    await waitFor(() => expect(publishWorkflow).toHaveBeenCalled())
    expect(createComponent).toHaveBeenCalled()
    await waitFor(() => expect(screen.getByText(/已发布不可变版本/)).toBeInTheDocument())
  })

  it('JSON 视图可导出并应用回结构化 state', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByTestId('canvas')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /JSON 视图/ }))
    expect(screen.getByLabelText('工作流 JSON 定义')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /应用 JSON/ }))
    await waitFor(() => expect(screen.getByTestId('canvas')).toBeInTheDocument())
  })
})
