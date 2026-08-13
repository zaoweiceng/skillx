import { StrictMode } from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import ChatPage from '@/pages/ChatPage'

const confirmComponent = vi.fn()
const getConversation = vi.fn()
const sendMessage = vi.fn()
const startAgentRun = vi.fn()
const getConversationRun = vi.fn()
const getTaskCheckpoint = vi.fn()
const listConversationRunEvents = vi.fn()
const getRun = vi.fn()
const createConversation = vi.fn()
const listConversations = vi.fn()
const updateConversationWorkflowMode = vi.fn()
const getHealth = vi.fn()

vi.mock('@/api/client', () => ({
  api: {
    createConversation: (...args: unknown[]) => createConversation(...args),
    listConversations: (...args: unknown[]) => listConversations(...args),
    updateConversationWorkflowMode: (...args: unknown[]) =>
      updateConversationWorkflowMode(...args),
    getConversation: (...args: unknown[]) => getConversation(...args),
    sendMessage: (...args: unknown[]) => sendMessage(...args),
    confirmComponent: (...args: unknown[]) => confirmComponent(...args),
    startAgentRun: (...args: unknown[]) => startAgentRun(...args),
    getConversationRun: (...args: unknown[]) => getConversationRun(...args),
    getTaskCheckpoint: (...args: unknown[]) => getTaskCheckpoint(...args),
    listConversationRunEvents: (...args: unknown[]) => listConversationRunEvents(...args),
    getRun: (...args: unknown[]) => getRun(...args),
    getHealth: (...args: unknown[]) => getHealth(...args),
    distillConversationRun: vi.fn(),
  },
  conversationRunUrl: (id: string) => `/api/conversation-runs/${id}`,
  taskCheckpointUrl: (id: string) => `/api/task-checkpoints/${id}`,
}))

const candidateMessage = {
  id: 'm1',
  role: 'assistant',
  content: {
    type: 'component_candidate',
    state: 'component_candidate',
    text: '发现匹配组件，请确认并补充参数后启动，或继续通用 Agent。',
    componentId: 'comp-1',
    componentName: '问候组件',
    workflowVersionId: 'v1',
    confidence: 0.75,
    reason: '描述词元命中 1',
    candidateSnapshotDigest: 'sha256:token',
    inputSchema: {
      type: 'object',
      properties: { name: { type: 'string' } },
      required: ['name'],
    },
    uiSchema: {},
  },
}

// 产生候选的用户消息，用于验证候选卡片按来源消息取 prompt。
const sourceUserMessage = {
  id: 'u0',
  role: 'user',
  content: { type: 'text', text: '生成 greeting' },
}

async function sendPrompt() {
  fireEvent.change(screen.getByLabelText('对话输入'), { target: { value: '生成 greeting' } })
  fireEvent.click(screen.getByRole('button', { name: /发送|处理中/ }))
}

// 直接以 /chat/:id 进入，跳过创建对话，聚焦既有会话渲染逻辑。
function renderAt(conversationId: string) {
  return render(
    <MemoryRouter initialEntries={[`/chat/${conversationId}`]}>
      <Routes>
        <Route path="/chat" element={<ChatPage />} />
        <Route path="/chat/:id" element={<ChatPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

const sourceFailureMessage = {
  id: 'source-message',
  role: 'assistant',
  content: {
    type: 'agent_run',
    state: 'failed',
    text: '通用 Agent 已进入队列，运行事件将实时更新。',
    conversationRunId: 'source-run',
    runType: 'agent_run',
  },
}

const continuationMessage = {
  id: 'continuation-message',
  role: 'assistant',
  content: {
    type: 'agent_run',
    state: 'queued',
    text: '已创建第 1 代失败接力，将从已冻结的剩余计划继续。',
    conversationRunId: 'continuation-run',
    runType: 'agent_run',
    sourceRunId: 'source-run',
    taskCheckpointId: 'checkpoint-12345678',
    continuationGeneration: 1,
  },
}

function checkpointSummary(chainStatus: string) {
  return {
    id: 'checkpoint-12345678',
    sourceId: 'source-run',
    status: chainStatus === 'succeeded' ? 'succeeded' : 'continuing',
    continuationRunId: 'continuation-run',
    continuationStatus: chainStatus === 'succeeded' ? 'succeeded' : 'running',
    continuationOutputDigest: chainStatus === 'succeeded' ? 'sha256:output' : null,
    continuationGeneration: 1,
    sourceCheckpointId: null,
    childCheckpointId: null,
    nextContinuationRunId: null,
    chainStatus,
    chainError: null,
    pendingQuestion: null,
    lineage: [
      {
        checkpointId: 'checkpoint-12345678',
        sourceRunId: 'source-run',
        continuationRunId: 'continuation-run',
        generation: 1,
        status: chainStatus === 'succeeded' ? 'succeeded' : 'continuing',
        continuationStatus: chainStatus === 'succeeded' ? 'succeeded' : 'running',
        childCheckpointId: null,
      },
    ],
  }
}

describe('ChatPage 候选组件闭环', () => {
  beforeEach(() => {
    confirmComponent.mockReset()
    getConversation.mockReset()
    sendMessage.mockReset()
    startAgentRun.mockReset()
    getConversationRun.mockReset()
    getTaskCheckpoint.mockReset()
    listConversationRunEvents.mockReset()
    getRun.mockReset()
    createConversation.mockReset()
    listConversations.mockReset()
    updateConversationWorkflowMode.mockReset()
    getHealth.mockReset()
    createConversation.mockResolvedValue({ id: 'conv-1', title: '新对话' })
    listConversations.mockResolvedValue({ items: [], nextCursor: null })
    getHealth.mockResolvedValue({ workflowSelectionMode: 'real' })
    updateConversationWorkflowMode.mockResolvedValue({
      id: 'conv-1',
      workflowMode: 'disabled',
      appliesFrom: 'next_turn',
      cancelledPendingSelections: 0,
    })
  })

  afterEach(() => {
    cleanup()
  })

  it('在 StrictMode 下进入 /chat 只创建一个对话', async () => {
    getConversation.mockResolvedValue({
      id: 'conv-1',
      title: '新对话',
      boundComponentId: null,
      workflowMode: 'enabled',
      messages: [],
      turns: [],
    })

    render(
      <StrictMode>
        <MemoryRouter initialEntries={['/chat']}>
          <Routes>
            <Route path="/chat" element={<ChatPage />} />
            <Route path="/chat/:id" element={<ChatPage />} />
          </Routes>
        </MemoryRouter>
      </StrictMode>,
    )

    await waitFor(() => expect(getConversation).toHaveBeenCalledWith('conv-1'))
    expect(createConversation).toHaveBeenCalledTimes(1)
  })

  it('真实语义 Runtime 缺失时预先提示自动选择不可用', async () => {
    getHealth.mockResolvedValue({ workflowSelectionMode: 'unavailable' })
    getConversation.mockResolvedValue({
      id: 'conv-1',
      title: '新对话',
      boundComponentId: null,
      workflowMode: 'enabled',
      messages: [],
      turns: [],
    })

    renderAt('conv-1')

    await waitFor(() =>
      expect(
        screen.getByText('自动选择不可用；请关闭工作流后继续通用 Agent'),
      ).toBeInTheDocument(),
    )
  })

  it('渲染候选卡片并支持确认后启动组件', async () => {
    sendMessage.mockResolvedValue({ userMessageId: 'u1', assistant: candidateMessage.content })
    getConversation.mockResolvedValue({
      id: 'conv-1',
      title: '新对话',
      boundComponentId: null,
      messages: [candidateMessage],
    })
    confirmComponent.mockResolvedValue({
      runId: 'run-9',
      assistant: { type: 'component_run', text: '组件已启动' },
    })

    renderAt('conv-1')
    await waitFor(() => expect(screen.getByLabelText('对话输入')).toBeEnabled())
    await sendPrompt()

    await waitFor(() => expect(screen.getByText(/候选组件/)).toBeInTheDocument())
    expect(screen.getByText(/置信度 75%/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '使用该组件' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '继续通用 Agent' })).toBeInTheDocument()
  })

  it('继续通用 Agent 会真正调用 Agent endpoint 并展示轨迹', async () => {
    sendMessage.mockResolvedValue({ userMessageId: 'u1', assistant: candidateMessage.content })
    // 会话含产生候选的用户消息，验证按来源消息取 prompt。
    getConversation.mockResolvedValue({
      id: 'conv-1',
      title: '新对话',
      boundComponentId: null,
      messages: [sourceUserMessage, candidateMessage],
    })
    startAgentRun.mockResolvedValue({
      conversationRunId: 'crun-1',
      status: 'succeeded',
      assistant: { type: 'agent_run', text: 'Agent 已完成', conversationRunId: 'crun-1' },
    })
    getConversationRun.mockResolvedValue({
      id: 'crun-1',
      conversationId: 'conv-1',
      status: 'succeeded',
      runType: 'agent_run',
      input: {},
      output: { reply: '完成' },
      error: null,
    })
    listConversationRunEvents.mockResolvedValue([
      { seq: 1, type: 'agent.run.created', payload: {} },
      { seq: 2, type: 'agent.plan', payload: { summary: '计划' } },
      { seq: 3, type: 'agent.run.succeeded', payload: { summary: { steps: 1, totalTokens: 20 } } },
    ])

    renderAt('conv-1')
    await waitFor(() => expect(screen.getByLabelText('对话输入')).toBeEnabled())
    await sendPrompt()

    await waitFor(() => expect(screen.getByText(/候选组件/)).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: '继续通用 Agent' }))
    await waitFor(() => expect(startAgentRun).toHaveBeenCalled())
    // 使用来源用户消息文本，而不是全局最近 prompt。
    expect(startAgentRun).toHaveBeenCalledWith('conv-1', '生成 greeting')
    expect(confirmComponent).not.toHaveBeenCalled()
    await waitFor(() => expect(screen.getByText('agent.run.succeeded')).toBeInTheDocument())
  })

  it('刷新进入 /chat/:id 直接恢复既有会话，不再创建新对话', async () => {
    getConversation.mockResolvedValue({
      id: 'conv-existing',
      title: '历史对话',
      boundComponentId: null,
      messages: [{ id: 'm0', role: 'user', content: { type: 'text', text: '历史输入' } }],
    })

    renderAt('conv-existing')
    // 路由已带 conversationId：直接按该 id 拉取会话，不调用 createConversation。
    await waitFor(() => expect(getConversation).toHaveBeenCalledWith('conv-existing'))
    expect(createConversation).not.toHaveBeenCalled()
    expect(screen.getByText('历史输入')).toBeInTheDocument()
  })

  it('Agent 运行未成功时不展示固化按钮，只提示状态', async () => {
    const runningAgentMessage = {
      id: 'a1',
      role: 'assistant',
      content: {
        type: 'agent_run',
        state: 'agent_run',
        text: '通用 Agent 已进入队列。',
        conversationRunId: 'crun-run',
        runType: 'agent_run',
      },
    }
    getConversation.mockResolvedValue({
      id: 'conv-1',
      title: '新对话',
      boundComponentId: null,
      messages: [runningAgentMessage],
    })
    getConversationRun.mockResolvedValue({
      id: 'crun-run',
      conversationId: 'conv-1',
      status: 'running',
      runType: 'agent_run',
      input: {},
      output: {},
      error: null,
    })
    listConversationRunEvents.mockResolvedValue([{ seq: 1, type: 'agent.run.created', payload: {} }])

    renderAt('conv-1')
    await waitFor(() => expect(screen.getByText(/仅 succeeded 可固化/)).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: '固化为组件' })).not.toBeInTheDocument()
  })

  it('sendMessage 直接返回自动 AgentRun 时兼容缺少 runType 的历史消息', async () => {
    const automaticAgentMessage = {
      id: 'auto-agent',
      role: 'assistant',
      content: {
        type: 'agent_run',
        state: 'queued',
        text: '已由通用 Agent 继续处理。',
        conversationRunId: 'crun-auto',
      },
    }
    getConversation
      .mockResolvedValueOnce({
        id: 'conv-1',
        title: '新对话',
        boundComponentId: null,
        messages: [],
      })
      .mockResolvedValue({
        id: 'conv-1',
        title: '新对话',
        boundComponentId: null,
        messages: [automaticAgentMessage],
      })
    sendMessage.mockResolvedValue({
      userMessageId: 'u-auto',
      assistant: automaticAgentMessage.content,
    })
    getConversationRun.mockResolvedValue({
      id: 'crun-auto',
      conversationId: 'conv-1',
      status: 'succeeded',
      runType: 'agent_run',
      input: {},
      output: { reply: '自动探索完成' },
      error: null,
      distillationEligibility: {
        eligible: true,
        actionCount: 1,
        reasonCode: 'ELIGIBLE',
      },
    })
    listConversationRunEvents.mockResolvedValue([
      { seq: 1, type: 'agent.run.succeeded', payload: {} },
    ])

    renderAt('conv-1')
    await waitFor(() => expect(screen.getByLabelText('对话输入')).toBeEnabled())
    fireEvent.change(screen.getByLabelText('对话输入'), {
      target: { value: '读取网页并总结' },
    })
    fireEvent.click(screen.getByRole('button', { name: /发送|处理中/ }))

    await waitFor(() => expect(screen.getByText('自动探索完成')).toBeInTheDocument())
    expect(screen.getByRole('button', { name: '固化为组件' })).toBeInTheDocument()
  })

  it('无工具成功回答显示不可固化原因而不是无效按钮', async () => {
    getConversation.mockResolvedValue({
      id: 'conv-1',
      title: '普通回答',
      boundComponentId: null,
      messages: [
        {
          id: 'plain-answer',
          role: 'assistant',
          content: {
            type: 'agent_run',
            text: '普通回答',
            conversationRunId: 'plain-run',
          },
        },
      ],
    })
    getConversationRun.mockResolvedValue({
      id: 'plain-run',
      conversationId: 'conv-1',
      status: 'succeeded',
      runType: 'agent_run',
      input: {},
      output: { reply: '普通回答' },
      error: null,
      distillationEligibility: {
        eligible: false,
        actionCount: 0,
        reasonCode: 'NO_REUSABLE_ACTIONS',
      },
    })
    listConversationRunEvents.mockResolvedValue([])

    renderAt('conv-1')
    await waitFor(() => expect(screen.getByText('本次没有可复用动作')).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: '固化为组件' })).not.toBeInTheDocument()
  })

  it('刷新后仍显示参数绑定失败及字段级诊断', async () => {
    getConversation.mockResolvedValue({
      id: 'conv-1',
      title: '失败已持久化',
      boundComponentId: null,
      messages: [
        {
          id: 'binding-error',
          role: 'assistant',
          content: {
            type: 'workflow_selection',
            state: 'error',
            code: 'SELECTION_BINDING_INCOMPLETE',
            text: '参数来源未通过验证',
            diagnostics: [
              {
                field: 'name',
                code: 'VALUE_NOT_IN_SOURCE',
                message: 'canonicalValue 不在 rawSpan 原文中',
              },
            ],
          },
        },
      ],
    })

    renderAt('conv-1')

    await waitFor(() => expect(screen.getByText('SELECTION_BINDING_INCOMPLETE')).toBeInTheDocument())
    expect(screen.getByLabelText('失败诊断')).toHaveTextContent('VALUE_NOT_IN_SOURCE')
    expect(screen.getByLabelText('失败诊断')).toHaveTextContent('name')
  })

  it('source failed 后自动发现 continuation 并展示最终正文', async () => {
    let conversationReads = 0
    let continuationReads = 0
    let checkpointReads = 0
    getConversation.mockImplementation(async () => {
      conversationReads += 1
      return {
        id: 'conv-1',
        title: '真实失败接力',
        boundComponentId: null,
        messages:
          conversationReads === 1
            ? [sourceFailureMessage]
            : [sourceFailureMessage, continuationMessage],
      }
    })
    getConversationRun.mockImplementation(async (runId: string) => {
      if (runId === 'source-run') {
        return {
          id: runId,
          conversationId: 'conv-1',
          status: 'failed',
          runType: 'agent_run',
          input: {},
          output: {},
          error: {
            code: 'DEPENDENCY_UNAVAILABLE',
            message: '上游暂不可用',
            taskCheckpointId: 'checkpoint-12345678',
          },
        }
      }
      continuationReads += 1
      const succeeded = continuationReads > 1
      return {
        id: runId,
        conversationId: 'conv-1',
        status: succeeded ? 'succeeded' : 'running',
        runType: 'agent_run',
        input: { continuationOfConversationRunId: 'source-run' },
        output: succeeded ? { reply: '恢复后的正文' } : {},
        error: null,
      }
    })
    getTaskCheckpoint.mockImplementation(async () => {
      checkpointReads += 1
      return checkpointSummary(checkpointReads > 1 ? 'succeeded' : 'continuing')
    })
    listConversationRunEvents.mockResolvedValue([])

    renderAt('conv-1')

    await waitFor(() => expect(screen.getByText('恢复后的正文')).toBeInTheDocument())
    expect(getConversation.mock.calls.length).toBeGreaterThanOrEqual(2)
    expect(screen.queryByText('上游暂不可用')).not.toBeInTheDocument()
    expect(screen.getAllByText('恢复后的正文')).toHaveLength(1)
    expect(screen.getAllByRole('link', { name: 'checkpoint checkpoi' })[0]).toHaveAttribute(
      'href',
      '/api/task-checkpoints/checkpoint-12345678',
    )
  })

  it('刷新后从持久消息恢复 continuation 结果且不重复源错误', async () => {
    getConversation.mockResolvedValue({
      id: 'conv-1',
      title: '已恢复的失败接力',
      boundComponentId: null,
      messages: [sourceFailureMessage, continuationMessage],
    })
    getConversationRun.mockImplementation(async (runId: string) =>
      runId === 'source-run'
        ? {
            id: runId,
            conversationId: 'conv-1',
            status: 'failed',
            runType: 'agent_run',
            input: {},
            output: {},
            error: {
              code: 'DEPENDENCY_UNAVAILABLE',
              message: '源 503',
              taskCheckpointId: 'checkpoint-12345678',
            },
          }
        : {
            id: runId,
            conversationId: 'conv-1',
            status: 'succeeded',
            runType: 'agent_run',
            input: { continuationOfConversationRunId: 'source-run' },
            output: { reply: '刷新后仍可见的恢复正文' },
            error: null,
          },
    )
    getTaskCheckpoint.mockResolvedValue(checkpointSummary('succeeded'))
    listConversationRunEvents.mockResolvedValue([])

    renderAt('conv-1')

    await waitFor(() =>
      expect(screen.getByText('刷新后仍可见的恢复正文')).toBeInTheDocument(),
    )
    expect(screen.getAllByText('刷新后仍可见的恢复正文')).toHaveLength(1)
    expect(screen.queryByText('源 503')).not.toBeInTheDocument()
  })

  it('接力完整性失败时区分最终错误与源错误', async () => {
    getConversation.mockResolvedValue({
      id: 'conv-1',
      title: '接力终态失败',
      boundComponentId: null,
      messages: [sourceFailureMessage],
    })
    getConversationRun.mockResolvedValue({
      id: 'source-run',
      conversationId: 'conv-1',
      status: 'failed',
      runType: 'agent_run',
      input: {},
      output: {},
      error: {
        code: 'DEPENDENCY_UNAVAILABLE',
        message: '源站点返回 503',
        taskCheckpointId: 'checkpoint-12345678',
      },
    })
    getTaskCheckpoint.mockResolvedValue({
      ...checkpointSummary('failed'),
      status: 'terminal',
      continuationRunId: null,
      continuationStatus: null,
      chainError: {
        code: 'CONTINUATION_INTEGRITY_FAILED',
        message: 'PlanSnapshot digest 不一致',
      },
      lineage: [
        {
          checkpointId: 'checkpoint-12345678',
          sourceRunId: 'source-run',
          continuationRunId: null,
          generation: 0,
          status: 'terminal',
          continuationStatus: null,
          childCheckpointId: null,
        },
      ],
    })
    listConversationRunEvents.mockResolvedValue([])

    renderAt('conv-1')

    await waitFor(() =>
      expect(screen.getByText('CONTINUATION_INTEGRITY_FAILED')).toBeInTheDocument(),
    )
    expect(screen.getByText('PlanSnapshot digest 不一致')).toBeInTheDocument()
    expect(screen.getByText(/源错误：DEPENDENCY_UNAVAILABLE/)).toHaveTextContent(
      '源站点返回 503',
    )
  })

  it('刷新后直接展示 Workflow succeeded 的持久输出且不重复', async () => {
    getConversation.mockResolvedValue({
      id: 'conv-1',
      title: '复用结果',
      boundComponentId: null,
      messages: [
        {
          id: 'workflow-1',
          role: 'assistant',
          content: {
            type: 'workflow_run',
            state: 'workflow_run',
            text: '选择已执行，后续由 Workflow Runtime 接管。',
            runId: 'run-1',
          },
        },
      ],
    })
    getRun.mockResolvedValue({
      id: 'run-1',
      status: 'succeeded',
      output: { reply: '第二次复用的新摘要' },
      error: null,
    })

    renderAt('conv-1')

    await waitFor(() => expect(screen.getByText('第二次复用的新摘要')).toBeInTheDocument())
    expect(screen.getAllByText('第二次复用的新摘要')).toHaveLength(1)
    expect(screen.getByRole('link', { name: '查看运行' })).toHaveAttribute(
      'href',
      '/runs/run-1',
    )
  })

  it('Workflow failed 显示稳定错误码和消息', async () => {
    getConversation.mockResolvedValue({
      id: 'conv-1',
      title: '失败结果',
      boundComponentId: null,
      messages: [
        {
          id: 'workflow-failed',
          role: 'assistant',
          content: {
            type: 'workflow_run',
            state: 'workflow_run',
            text: '选择已执行，后续由 Workflow Runtime 接管。',
            runId: 'run-failed',
          },
        },
      ],
    })
    getRun.mockResolvedValue({
      id: 'run-failed',
      status: 'failed',
      output: null,
      error: { code: 'DEPENDENCY_UNAVAILABLE', message: '上游暂不可用' },
    })

    renderAt('conv-1')

    await waitFor(() => expect(screen.getByText('DEPENDENCY_UNAVAILABLE')).toBeInTheDocument())
    expect(screen.getByText('上游暂不可用')).toBeInTheDocument()
  })

  it('展示持久历史并切换当前对话的工作流模式', async () => {
    getConversation.mockResolvedValue({
      id: 'conv-1',
      title: '当前对话',
      boundComponentId: null,
      workflowMode: 'enabled',
      turns: [],
      messages: [],
    })
    listConversations.mockResolvedValue({
      items: [
        {
          id: 'conv-1',
          title: '当前对话',
          preview: '上海天气如何',
          lastMessageAt: '2026-08-10T10:00:00Z',
          status: 'idle',
          workflowMode: 'enabled',
        },
      ],
      nextCursor: null,
    })

    renderAt('conv-1')

    await waitFor(() => expect(screen.getByText('当前对话')).toBeInTheDocument())
    const toggle = screen.getByRole('button', { name: '工作流：开启' })
    fireEvent.click(toggle)
    await waitFor(() =>
      expect(updateConversationWorkflowMode).toHaveBeenCalledWith('conv-1', 'disabled'),
    )
    expect(screen.getByRole('button', { name: '工作流：关闭' })).toHaveAttribute(
      'aria-pressed',
      'false',
    )
  })
})
