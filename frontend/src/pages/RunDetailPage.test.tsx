import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import RunDetailPage from '@/pages/RunDetailPage'

const getRun = vi.fn()
const approve = vi.fn()
const reject = vi.fn()
const applyWorkflowPatch = vi.fn()

vi.mock('@/api/client', () => ({
  api: {
    getRun: (...args: unknown[]) => getRun(...args),
    approve: (...args: unknown[]) => approve(...args),
    reject: (...args: unknown[]) => reject(...args),
    applyWorkflowPatch: (...args: unknown[]) => applyWorkflowPatch(...args),
    cancelRun: vi.fn(),
  },
  eventStreamUrl: () => 'http://localhost/stream',
}))

// EventSource 在 jsdom 中不存在，且流不是本测试关注点。
vi.mock('@/hooks/useRunStream', () => ({
  useRunStream: () => undefined,
  useRunEvents: (selector: (state: { events: Record<string, unknown[]> }) => unknown) =>
    selector({ events: {} }),
}))

const baseRun = {
  id: 'run-1',
  workflowVersionId: 'v1',
  componentId: 'c1',
  status: 'succeeded',
  input: { name: 'Alice' },
  output: { message: '你好，Alice' },
  error: null,
  createdAt: '2026-08-03T00:00:00Z',
  startedAt: '2026-08-03T00:00:00Z',
  finishedAt: '2026-08-03T00:00:01Z',
  nodes: [
    {
      id: 'n1',
      nodeId: 'format',
      type: 'transform',
      attempt: 1,
      status: 'succeeded',
      input: {},
      output: { text: '你好，Alice' },
      error: null,
      durationMs: 42,
      metrics: { totalTokens: 0 },
    },
  ],
  approvals: [],
  artifacts: [],
  dependencyVersions: {
    workflowVersionId: 'v1',
    workflowDigest: 'sha256:abc',
    executionClass: 'zero_llm',
    capabilityLocks: [],
    mcpLocks: [],
  },
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/runs/run-1']}>
      <Routes>
        <Route path="/runs/:id" element={<RunDetailPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('RunDetailPage 可观测性与审批', () => {
  beforeEach(() => {
    getRun.mockReset()
    approve.mockReset()
    reject.mockReset()
    applyWorkflowPatch.mockReset()
  })

  afterEach(() => cleanup())

  it('展示节点耗时、指标与依赖版本', async () => {
    getRun.mockResolvedValue(baseRun)
    renderPage()
    await waitFor(() => expect(screen.getByText('运行详情')).toBeInTheDocument())
    expect(screen.getByText(/42ms/)).toBeInTheDocument()
    expect(screen.getByText('依赖版本')).toBeInTheDocument()
    expect(screen.getByText(/totalTokens/)).toBeInTheDocument()
  })

  it('对待审批 Run 可确认', async () => {
    getRun.mockResolvedValue({
      ...baseRun,
      status: 'waiting_approval',
      approvals: [
        {
          id: 'ap-1',
          nodeId: 'submit',
          status: 'pending',
          snapshotDigest: 'sha256:d',
          snapshot: { title: '确认提交', description: '请确认', payload: {}, effect: 'form.submit' },
        },
      ],
    })
    approve.mockResolvedValue({ id: 'ap-1', status: 'approved' })
    renderPage()
    await waitFor(() => expect(screen.getByText('确认提交')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /确认继续/ }))
    await waitFor(() => expect(approve).toHaveBeenCalledWith('ap-1', 'sha256:d'))
  })

  it('恢复运行展示检查点接力视图（已完成节点/剩余子图）', async () => {
    getRun.mockResolvedValue({
      ...baseRun,
      status: 'succeeded',
      checkpoints: [
        {
          id: 'ck-1',
          status: 'applied',
          failedNode: 'risky',
          attempt: 1,
          digest: 'sha256:c',
          completedNodes: ['sensitive'],
          remainingSubgraph: ['risky'],
          committedSideEffects: [{ nodeId: 'sensitive', idempotent: false }],
          decision: { action: 'provide_failed_output' },
        },
      ],
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('检查点接力')).toBeInTheDocument())
    expect(screen.getByText(/失败节点 risky/)).toBeInTheDocument()
    expect(screen.getByText('sensitive')).toBeInTheDocument()
  })

  it('失败运行可对失败节点提出受控补丁并展示 replay 结果', async () => {
    getRun.mockResolvedValue({
      ...baseRun,
      status: 'failed',
      output: null,
      error: { code: 'TIMEOUT', message: '超时' },
      nodes: [
        {
          id: 'n1',
          nodeId: 'work',
          type: 'transform',
          attempt: 1,
          status: 'failed',
          input: { timeoutSeconds: 30 },
          output: null,
          error: { code: 'TIMEOUT', message: '超时' },
          durationMs: 100,
          metrics: {},
        },
      ],
    })
    applyWorkflowPatch.mockResolvedValue({
      draftWorkflowId: 'draft-9',
      baseVersionId: 'v1',
      diagnostics: [],
      publishable: true,
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('受控版本修订')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /提出补丁草稿/ }))
    await waitFor(() =>
      expect(applyWorkflowPatch).toHaveBeenCalledWith('v1', {
        nodePatches: [{ nodeId: 'work', changes: { timeoutSeconds: 60 } }],
        reason: '对失败节点 work 提出受控超时补丁',
      }),
    )
    await waitFor(() => expect(screen.getByText(/打开草稿审核 diff 并发布/)).toBeInTheDocument())
  })
})
