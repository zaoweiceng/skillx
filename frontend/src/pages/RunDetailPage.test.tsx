import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useRunEvents } from '@/hooks/useRunStream'
import RunDetailPage from '@/pages/RunDetailPage'

const getRun = vi.fn()
const listRunEvents = vi.fn()
const approve = vi.fn()
const reject = vi.fn()
const applyWorkflowPatch = vi.fn()
const proposeRevision = vi.fn()
const eventSources: FakeEventSource[] = []

class FakeEventSource {
  listeners = new Map<string, EventListener>()
  closed = false

  constructor(public readonly url: string) {
    eventSources.push(this)
  }

  addEventListener(type: string, listener: EventListener) {
    this.listeners.set(type, listener)
  }

  removeEventListener(type: string) {
    this.listeners.delete(type)
  }

  close() {
    this.closed = true
  }
}

vi.mock('@/api/client', () => ({
  api: {
    getRun: (...args: unknown[]) => getRun(...args),
    listRunEvents: (...args: unknown[]) => listRunEvents(...args),
    approve: (...args: unknown[]) => approve(...args),
    reject: (...args: unknown[]) => reject(...args),
    applyWorkflowPatch: (...args: unknown[]) => applyWorkflowPatch(...args),
    proposeRevision: (...args: unknown[]) => proposeRevision(...args),
    cancelRun: vi.fn(),
  },
  eventStreamUrl: (_id: string, after: number) =>
    `http://localhost/stream?after=${after}`,
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
  runtimeSnapshot: {
    runtimeSelection: 'agent_fallback',
    runtimeUsed: null,
    runtimeCalls: [],
    modelCalled: false,
    totalTokens: 0,
    fallbackReason: 'LLM_RUNTIME_UNCONFIGURED',
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
    listRunEvents.mockReset()
    listRunEvents.mockResolvedValue([])
    approve.mockReset()
    reject.mockReset()
    applyWorkflowPatch.mockReset()
    proposeRevision.mockReset()
    eventSources.splice(0)
    useRunEvents.setState({ events: {} })
    vi.stubGlobal('EventSource', FakeEventSource)
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('终态 Run 恢复持久事件且不创建 EventSource', async () => {
    getRun.mockResolvedValue(baseRun)
    listRunEvents.mockResolvedValue([
      {
        id: 'event-1',
        seq: 1,
        type: 'run.completed',
        payload: { status: 'succeeded' },
      },
    ])
    renderPage()

    await waitFor(() =>
      expect(screen.getByText('run.completed')).toBeInTheDocument(),
    )
    expect(listRunEvents).toHaveBeenCalledWith('run-1', 0, 200)
    expect(screen.getByText('实时流已关闭')).toBeInTheDocument()
    expect(screen.getByText('模型未调用 / 0 Token')).toBeInTheDocument()
    expect(screen.queryByText('实际调用：agent_fallback')).not.toBeInTheDocument()
    expect(eventSources).toHaveLength(0)
  })

  it('历史加载后从最后 seq 建立 SSE 并去重排序', async () => {
    getRun.mockResolvedValue({ ...baseRun, status: 'running', output: null })
    listRunEvents.mockResolvedValue([
      { seq: 2, type: 'node.started', payload: {} },
      { seq: 1, type: 'run.started', payload: {} },
    ])
    renderPage()

    await waitFor(() => expect(eventSources).toHaveLength(1))
    expect(eventSources[0].url).toBe('http://localhost/stream?after=2')
    expect(screen.getByText('run.started')).toBeInTheDocument()
    expect(screen.getByText('node.started')).toBeInTheDocument()
  })

  it('收到终态事件后立即关闭 EventSource', async () => {
    getRun.mockResolvedValue({ ...baseRun, status: 'running', output: null })
    renderPage()

    await waitFor(() => expect(eventSources).toHaveLength(1))
    const stream = eventSources[0]
    const listener = stream.listeners.get('run.event')
    for (const [seq, type] of [
      [1, 'mcp.call.completed'],
      [2, 'artifact.created'],
    ] as const) {
      listener?.(
        new MessageEvent('run.event', {
          data: JSON.stringify({ runId: 'run-1', seq, type, payload: {} }),
        }),
      )
    }
    listener?.(
      new MessageEvent('run.event', {
        data: JSON.stringify({
          runId: 'run-1',
          seq: 3,
          type: 'run.completed',
          payload: { status: 'succeeded' },
        }),
      }),
    )
    expect(stream.closed).toBe(false)
    listener?.(
      new MessageEvent('run.event', {
        data: JSON.stringify({
          runId: 'run-1',
          seq: 4,
          type: 'presentation.completed',
          payload: { status: 'completed' },
        }),
      }),
    )

    await waitFor(() => expect(stream.closed).toBe(true))
    expect(useRunEvents.getState().events['run-1'].map((item) => item.seq)).toEqual([
      1, 2, 3, 4,
    ])
    expect(eventSources).toHaveLength(1)
  })

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
      failureObservations: [
        {
          id: 'observation-1',
          nodeId: 'work',
          errorCode: 'CONTRACT_VIOLATION',
          classification: 'contract_drift',
          occurrence: 2,
          recovered: false,
          baselineReproduced: true,
          replayEvidence: [],
        },
      ],
    })
    proposeRevision.mockResolvedValue({
      proposalId: 'proposal-1',
      proposalDigest: 'sha256:proposal',
      proposal: { nodePatches: [] },
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
        sourceRunId: baseRun.id,
        proposalId: 'proposal-1',
        proposalDigest: 'sha256:proposal',
      }),
    )
    await waitFor(() => expect(screen.getByText(/打开草稿审核 diff 并发布/)).toBeInTheDocument())
  })
})
