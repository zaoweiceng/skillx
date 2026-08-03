import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import ChatPage from '@/pages/ChatPage'

const confirmComponent = vi.fn()
const getConversation = vi.fn()
const sendMessage = vi.fn()
const startAgentRun = vi.fn()
const getConversationRun = vi.fn()
const listConversationRunEvents = vi.fn()

vi.mock('@/api/client', () => ({
  api: {
    createConversation: () => Promise.resolve({ id: 'conv-1', title: '新对话' }),
    getConversation: (...args: unknown[]) => getConversation(...args),
    sendMessage: (...args: unknown[]) => sendMessage(...args),
    confirmComponent: (...args: unknown[]) => confirmComponent(...args),
    startAgentRun: (...args: unknown[]) => startAgentRun(...args),
    getConversationRun: (...args: unknown[]) => getConversationRun(...args),
    listConversationRunEvents: (...args: unknown[]) => listConversationRunEvents(...args),
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

async function sendPrompt() {
  fireEvent.change(screen.getByLabelText('对话输入'), { target: { value: '生成 greeting' } })
  fireEvent.click(screen.getByRole('button', { name: /发送|处理中/ }))
}

describe('ChatPage 候选组件闭环', () => {
  beforeEach(() => {
    confirmComponent.mockReset()
    getConversation.mockReset()
    sendMessage.mockReset()
    startAgentRun.mockReset()
    getConversationRun.mockReset()
    listConversationRunEvents.mockReset()
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

    render(
      <MemoryRouter>
        <ChatPage />
      </MemoryRouter>,
    )
    await waitFor(() => expect(screen.getByLabelText('对话输入')).toBeEnabled())
    await sendPrompt()

    await waitFor(() => expect(screen.getByText(/候选组件/)).toBeInTheDocument())
    expect(screen.getByText(/置信度 75%/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '使用该组件' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '继续通用 Agent' })).toBeInTheDocument()
  })

  it('继续通用 Agent 会真正调用 Agent endpoint 并展示轨迹', async () => {
    sendMessage.mockResolvedValue({ userMessageId: 'u1', assistant: candidateMessage.content })
    getConversation.mockResolvedValue({
      id: 'conv-1',
      title: '新对话',
      boundComponentId: null,
      messages: [candidateMessage],
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

    render(
      <MemoryRouter>
        <ChatPage />
      </MemoryRouter>,
    )
    await waitFor(() => expect(screen.getByLabelText('对话输入')).toBeEnabled())
    await sendPrompt()

    await waitFor(() => expect(screen.getByText(/候选组件/)).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: '继续通用 Agent' }))
    await waitFor(() => expect(startAgentRun).toHaveBeenCalled())
    expect(confirmComponent).not.toHaveBeenCalled()
    await waitFor(() => expect(screen.getByText('agent.run.succeeded')).toBeInTheDocument())
  })
})
