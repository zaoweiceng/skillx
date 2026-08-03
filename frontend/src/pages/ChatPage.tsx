import Form from '@rjsf/core'
import validator from '@rjsf/validator-ajv8'
import { Bot, CornerDownLeft, Link2Off, MessageSquareText } from 'lucide-react'
import { FormEvent, useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { api, ChatMessageContent, ConversationRunEvent } from '@/api/client'

type Message = {
  id: string
  role: string
  content: ChatMessageContent
}

export default function ChatPage() {
  const [conversationId, setConversationId] = useState('')
  const [messages, setMessages] = useState<Message[]>([])
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const [error, setError] = useState('')
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  // 候选卡片补参状态，按消息 id 索引。
  const [candidateInputs, setCandidateInputs] = useState<Record<string, Record<string, unknown>>>({})
  const [confirming, setConfirming] = useState('')
  const [startingAgent, setStartingAgent] = useState(false)
  // 最近一次用户输入，用于「继续/启动通用 Agent」时复用原始任务描述。
  const [lastPrompt, setLastPrompt] = useState('')
  // 轨迹抽屉：追踪最近一次通用 Agent 运行及其规范化事件。
  const [activeRunId, setActiveRunId] = useState('')
  const [runStatus, setRunStatus] = useState('')
  const [traceEvents, setTraceEvents] = useState<ConversationRunEvent[]>([])

  useEffect(() => {
    api
      .createConversation('新对话')
      .then((item) => setConversationId(item.id))
      .catch((reason: Error) => setError(reason.message))
  }, [])

  const refresh = useCallback(async () => {
    const conversation = await api.getConversation(conversationId)
    setMessages(conversation.messages)
    // 从服务端消息恢复最近一次 Agent 运行，刷新后仍可展示轨迹，不依赖本地 state。
    const latestAgent = [...conversation.messages]
      .reverse()
      .find((message) => message.content.conversationRunId && message.content.runType === 'agent_run')
    if (latestAgent?.content.conversationRunId) {
      setActiveRunId(latestAgent.content.conversationRunId)
    }
  }, [conversationId])

  // 订阅活动 Agent 运行的规范化事件，直到进入终态。
  useEffect(() => {
    if (!activeRunId) return
    let cancelled = false
    async function poll() {
      try {
        const [summary, events] = await Promise.all([
          api.getConversationRun(activeRunId),
          api.listConversationRunEvents(activeRunId),
        ])
        if (cancelled) return
        setRunStatus(summary.status)
        setTraceEvents(events)
        if (summary.status === 'running') {
          window.setTimeout(poll, 800)
        }
      } catch (reason) {
        if (!cancelled) setError(reason instanceof Error ? reason.message : '加载轨迹失败')
      }
    }
    poll()
    return () => {
      cancelled = true
    }
  }, [activeRunId])

  async function send(event: FormEvent) {
    event.preventDefault()
    if (!input.trim() || !conversationId) return
    const text = input.trim()
    setInput('')
    setLastPrompt(text)
    setSending(true)
    const optimistic: Message = {
      id: `local-${Date.now()}`,
      role: 'user',
      content: { type: 'text', text },
    }
    setMessages((current) => [...current, optimistic])
    try {
      await api.sendMessage(conversationId, text)
      await refresh()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '发送失败')
    } finally {
      setSending(false)
    }
  }

  async function startComponent(message: Message) {
    const content = message.content
    if (!content.componentId || !content.candidateSnapshotDigest) return
    setConfirming(message.id)
    setError('')
    try {
      await api.confirmComponent(conversationId, {
        componentId: content.componentId,
        candidateSnapshotDigest: content.candidateSnapshotDigest,
        inputs: candidateInputs[message.id] ?? {},
      })
      await refresh()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '组件启动失败')
    } finally {
      setConfirming('')
    }
  }

  // 「继续通用 Agent」/「启动通用 Agent」：真正调用 Agent endpoint，不再只隐藏卡片。
  async function startAgent(prompt: string) {
    if (!conversationId || !prompt.trim()) return
    setStartingAgent(true)
    setError('')
    try {
      const result = await api.startAgentRun(conversationId, prompt.trim())
      setActiveRunId(result.conversationRunId)
      await refresh()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '通用 Agent 启动失败')
    } finally {
      setStartingAgent(false)
    }
  }

  async function distill(conversationRunId: string) {
    try {
      const result = await api.distillConversationRun(conversationRunId)
      setDrafts((current) => ({
        ...current,
        [conversationRunId]: result.draftWorkflowId,
      }))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '固化失败')
    }
  }

  function lastUserPrompt(): string {
    if (lastPrompt) return lastPrompt
    const lastUser = [...messages].reverse().find((message) => message.role === 'user')
    return lastUser?.content.text ?? ''
  }

  function renderCandidate(message: Message) {
    const content = message.content
    return (
      <div className="candidate-card">
        <div className="candidate-meta">
          <strong>候选组件：{content.componentName}</strong>
          {typeof content.confidence === 'number' && (
            <span className="effect-label">置信度 {(content.confidence * 100).toFixed(0)}%</span>
          )}
          {content.reason && <span className="candidate-reason">匹配依据：{content.reason}</span>}
        </div>
        {content.inputSchema && (
          <Form
            schema={content.inputSchema}
            uiSchema={content.uiSchema ?? {}}
            validator={validator}
            formData={candidateInputs[message.id] ?? {}}
            onChange={(event) =>
              setCandidateInputs((current) => ({
                ...current,
                [message.id]: (event.formData as Record<string, unknown>) ?? {},
              }))
            }
            onSubmit={() => startComponent(message)}
          >
            <div className="candidate-actions">
              <button
                className="button primary"
                type="submit"
                disabled={confirming === message.id || !content.candidateSnapshotDigest}
              >
                {confirming === message.id ? '正在启动...' : '使用该组件'}
              </button>
              <button
                className="button ghost"
                type="button"
                disabled={startingAgent}
                onClick={() => startAgent(lastUserPrompt())}
              >
                {startingAgent ? '正在启动 Agent...' : '继续通用 Agent'}
              </button>
            </div>
          </Form>
        )}
      </div>
    )
  }

  function renderTrace() {
    if (!activeRunId || traceEvents.length === 0) {
      return (
        <div className="trace-empty">
          <span className="pulse-ring" />
          <p>Agent 启动后，这里只展示可审计的行动摘要与 Tool 事件。</p>
        </div>
      )
    }
    return (
      <ol className="trace-timeline" aria-label="Agent 轨迹">
        {traceEvents.map((event) => (
          <li className="trace-item" key={event.seq}>
            <span className="trace-type">{event.type}</span>
            {typeof event.payload.tool === 'string' && (
              <span className="trace-tool">工具 {event.payload.tool as string}</span>
            )}
            {typeof event.payload.summary === 'object' && event.payload.summary !== null && (
              <span className="trace-summary">
                步数 {(event.payload.summary as Record<string, unknown>).steps as number} · Tokens{' '}
                {(event.payload.summary as Record<string, unknown>).totalTokens as number}
              </span>
            )}
            {typeof event.payload.error === 'object' && event.payload.error !== null && (
              <span className="error-code">
                {(event.payload.error as Record<string, unknown>).code as string}
              </span>
            )}
          </li>
        ))}
      </ol>
    )
  }

  return (
    <div className="chat-page">
      <header className="chat-header">
        <div>
          <span className="eyebrow">GENERAL AGENT WORKSPACE</span>
          <h1>通用对话</h1>
        </div>
        <div className="context-pill">
          <Link2Off size={14} />
          未绑定组件
        </div>
      </header>
      <div className="chat-layout">
        <section className="message-stream">
          {messages.length === 0 && (
            <div className="chat-empty">
              <span className="chat-empty-icon">
                <MessageSquareText size={28} />
              </span>
              <h2>探索一次，固化复用</h2>
              <p>
                通用输入始终交给 LLM/Agent 理解，不使用城市名等静态规则直接启动组件。
              </p>
              <div className="prompt-suggestions">
                <button onClick={() => setInput('帮我梳理一个重复任务的执行步骤')}>
                  梳理重复任务
                </button>
                <button onClick={() => setInput('分析现有组件是否适合我的任务')}>
                  匹配已有组件
                </button>
              </div>
            </div>
          )}
          {messages.map((message) => (
            <article className={`message ${message.role}`} key={message.id}>
              <span className="message-avatar">
                {message.role === 'assistant' ? <Bot size={16} /> : '你'}
              </span>
              <div>
                <small>{message.role === 'assistant' ? 'SkillX Agent' : '用户'}</small>
                <p>{message.content.text}</p>
                {message.content.code && (
                  <span className="error-code">{message.content.code}</span>
                )}
                {message.content.type === 'component_candidate' && renderCandidate(message)}
                {message.content.offerAgentRun && (
                  <button
                    className="button ghost"
                    type="button"
                    disabled={startingAgent}
                    onClick={() => startAgent(lastUserPrompt())}
                  >
                    {startingAgent ? '正在启动 Agent...' : '启动通用 Agent 探索'}
                  </button>
                )}
                {message.content.runId && (
                  <Link className="button ghost" to={`/runs/${message.content.runId}`}>
                    查看运行
                  </Link>
                )}
                {message.content.conversationRunId &&
                  message.content.runType === 'agent_run' &&
                  (drafts[message.content.conversationRunId] ? (
                    <Link
                      className="button ghost"
                      to={`/workflows/${drafts[message.content.conversationRunId]}/edit`}
                    >
                      打开固化草稿
                    </Link>
                  ) : (
                    <button
                      className="button ghost"
                      onClick={() => distill(message.content.conversationRunId!)}
                    >
                      固化为组件
                    </button>
                  ))}
              </div>
            </article>
          ))}
        </section>
        <aside className="trace-drawer">
          <div className="panel-heading">
            <div>
              <span className="eyebrow">EXECUTION TRACE</span>
              <h2>执行轨迹</h2>
            </div>
            {runStatus && <span className={`status-badge ${runStatus}`}>{runStatus}</span>}
          </div>
          {renderTrace()}
        </aside>
      </div>
      <form className="chat-composer" onSubmit={send}>
        <textarea
          aria-label="对话输入"
          placeholder="描述一个需要探索或固化的任务..."
          value={input}
          onChange={(event) => setInput(event.target.value)}
          rows={2}
        />
        <button type="submit" disabled={sending || !conversationId}>
          <CornerDownLeft size={17} />
          <span>{sending ? '处理中' : '发送'}</span>
        </button>
      </form>
      {error && <div className="chat-error">{error}</div>}
    </div>
  )
}
