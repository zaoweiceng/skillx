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
      draftDigest: 'sha256:initial',
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
      draftDigest: 'sha256:x',
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
    expect(publishWorkflow).toHaveBeenCalledWith(
      'wf-1',
      2,
      'sha256:x',
      undefined,
    )
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

  it('展示 Presentation 提议依据并记录明确接受决定', async () => {
    const withProposal: WorkflowDefinition = {
      ...definition,
      metadata: {
        ...definition.metadata,
        presentationPolicyProposal: {
          source: 'distillation_agent',
          resultAudience: 'end_user',
          consumer: 'human',
          terminalOrFragment: 'terminal',
          enabled: true,
          producer: 'llm',
          prompt: '只整理规范 JSON',
          language: 'zh-CN',
          maxLength: 1200,
          reason: '任务结果直接交给用户阅读',
          evidence: ['consumer=human'],
          confidence: 0.88,
        },
        presentationPolicyDecision: { status: 'pending' },
      },
    }
    getWorkflow.mockResolvedValue({
      id: 'wf-1',
      name: 'greeting',
      draftRevision: 1,
      definition: withProposal,
      createdAt: '',
      updatedAt: '',
    })
    updateDraft.mockImplementation(
      (_id: string, _revision: number, nextDefinition: WorkflowDefinition) =>
        Promise.resolve({
          id: 'wf-1',
          name: 'greeting',
          draftRevision: 2,
          definition: nextDefinition,
          createdAt: '',
          updatedAt: '',
        }),
    )

    renderPage()
    await waitFor(() => expect(screen.getByText('固化 Agent 提议')).toBeInTheDocument())
    expect(screen.getByText('任务结果直接交给用户阅读')).toBeInTheDocument()
    expect(screen.getByText('consumer=human')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '接受提议' }))
    fireEvent.click(screen.getByRole('button', { name: /^保存$/ }))
    await waitFor(() => expect(updateDraft).toHaveBeenCalled())
    const saved = updateDraft.mock.calls.at(-1)?.[2] as WorkflowDefinition
    expect(saved.metadata.presentationPolicyDecision).toMatchObject({
      status: 'accepted',
      reviewer: 'local-user',
    })
    expect(saved.metadata.presentationPolicy).toMatchObject({
      enabled: true,
      producer: 'llm',
    })
  })

  it('为执行节点配置显式 fallback（勾选错误码后写入 target=agent 节点）', async () => {
    // 含 agent 节点的定义：transform 可选择 fallback 目标。
    const withAgent: WorkflowDefinition = {
      ...definition,
      spec: {
        ...definition.spec,
        nodes: [
          { id: 'input', type: 'input', displayName: '输入' },
          { id: 'work', type: 'transform', displayName: '处理', config: {}, input: {} },
          { id: 'recovery', type: 'agent', displayName: '恢复', config: {}, input: {} },
          { id: 'output', type: 'output', displayName: '输出' },
        ],
      },
    }
    getWorkflow.mockResolvedValue({
      id: 'wf-1',
      name: 'greeting',
      draftRevision: 1,
      definition: withAgent,
      createdAt: '',
      updatedAt: '',
    })
    updateDraft.mockResolvedValue({
      id: 'wf-1',
      name: 'greeting',
      draftRevision: 2,
      definition: withAgent,
      createdAt: '',
      updatedAt: '',
    })
    renderPage()
    await waitFor(() => expect(screen.getByTestId('canvas')).toBeInTheDocument())
    // 选中 work 节点：调色板占位无法点选，直接通过 JSON 视图往返不便，改用 Inspector。
    // EditableCanvas 被 mock，需借助 onSelectNode——此处通过点击调色板新增 transform 再选中。
    // 简化：直接断言 fallback 勾选框存在需先选中节点。这里选择新增一个 transform 后选中它。
    fireEvent.click(screen.getByRole('button', { name: /^.*transform.*$/ }))
    // 新增后 selectedNodeId 指向新节点，Inspector 渲染 fallback 编辑区（有 agent 目标）。
    await waitFor(() =>
      expect(screen.getByText(/显式失败接力/)).toBeInTheDocument(),
    )
    fireEvent.click(screen.getByLabelText('DEPENDENCY_UNAVAILABLE'))
    // 勾选后出现恢复目标下拉，且默认指向 agent 节点 recovery。
    const targetSelect = screen.getByLabelText('fallback 目标') as HTMLSelectElement
    expect(targetSelect.value).toBe('recovery')
  })
})
