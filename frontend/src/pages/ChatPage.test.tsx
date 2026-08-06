import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import ChatPage from '@/pages/ChatPage'

const confirmComponent = vi.fn()
const getConversation = vi.fn()
const sendMessage = vi.fn()
const startAgentRun = vi.fn()
const getConversationRun = vi.fn()
const listConversationRunEvents = vi.fn()
const getRun = vi.fn()
const createConversation = vi.fn()

vi.mock('@/api/client', () => ({
  api: {
    createConversation: (...args: unknown[]) => createConversation(...args),
    getConversation: (...args: unknown[]) => getConversation(...args),
    sendMessage: (...args: unknown[]) => sendMessage(...args),
    confirmComponent: (...args: unknown[]) => confirmComponent(...args),
    startAgentRun: (...args: unknown[]) => startAgentRun(...args),
    getConversationRun: (...args: unknown[]) => getConversationRun(...args),
    listConversationRunEvents: (...args: unknown[]) => listConversationRunEvents(...args),
    getRun: (...args: unknown[]) => getRun(...args),
    distillConversationRun: vi.fn(),
  },
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

describe('ChatPage 候选组件闭环', () => {
  beforeEach(() => {
    confirmComponent.mockReset()
    getConversation.mockReset()
    sendMessage.mockReset()
    startAgentRun.mockReset()
    getConversationRun.mockReset()
    listConversationRunEvents.mockReset()
    getRun.mockReset()
    createConversation.mockReset()
    createConversation.mockResolvedValue({ id: 'conv-1', title: '新对话' })
  })

  afterEach(() => {
    cleanup()
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
})
