import Form from '@rjsf/core'
import validator from '@rjsf/validator-ajv8'
import { Bot, CornerDownLeft, Link2Off, MessageSquareText, Plus } from 'lucide-react'
import { FormEvent, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { api, ChatMessageContent } from '@/api/client'

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
  // 候选卡片补参与降级状态，按消息 id 索引。
  const [candidateInputs, setCandidateInputs] = useState<Record<string, Record<string, unknown>>>({})
  const [dismissed, setDismissed] = useState<Record<string, boolean>>({})
  const [confirming, setConfirming] = useState('')

  useEffect(() => {
    api
      .createConversation('新对话')
      .then((item) => setConversationId(item.id))
      .catch((reason: Error) => setError(reason.message))
  }, [])

  async function refresh() {
    const conversation = await api.getConversation(conversationId)
    setMessages(conversation.messages)
  }

  async function send(event: FormEvent) {
    event.preventDefault()
    if (!input.trim() || !conversationId) return
    const text = input.trim()
    setInput('')
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
    if (!content.componentId || !content.candidateToken) return
    setConfirming(message.id)
    setError('')
    try {
      await api.confirmComponent(conversationId, {
        componentId: content.componentId,
        candidateToken: content.candidateToken,
        inputs: candidateInputs[message.id] ?? {},
      })
      await refresh()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '组件启动失败')
    } finally {
      setConfirming('')
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

  function renderCandidate(message: Message) {
    const content = message.content
    if (dismissed[message.id]) {
      return <span className="effect-label">已选择继续通用 Agent，未启动组件。</span>
    }
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
                disabled={confirming === message.id || !content.candidateToken}
              >
                {confirming === message.id ? '正在启动...' : '使用该组件'}
              </button>
              <button
                className="button ghost"
                type="button"
                onClick={() => setDismissed((current) => ({ ...current, [message.id]: true }))}
              >
                继续通用 Agent
              </button>
            </div>
          </Form>
        )}
      </div>
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
                {message.content.runId && (
                  <Link className="button ghost" to={`/runs/${message.content.runId}`}>
                    查看运行
                  </Link>
                )}
                {message.content.conversationRunId &&
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
            <Plus size={16} />
          </div>
          <div className="trace-empty">
            <span className="pulse-ring" />
            <p>Agent 启动后，这里只展示可审计的行动摘要与 Tool 事件。</p>
          </div>
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
