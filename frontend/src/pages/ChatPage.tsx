import { Bot, CornerDownLeft, Link2Off, MessageSquareText, Plus } from 'lucide-react'
import { FormEvent, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '@/api/client'

type Message = {
  id: string
  role: string
  content: {
    type: string
    text: string
    code?: string
    componentId?: string
    componentName?: string
    runId?: string
    conversationRunId?: string
    required?: string[]
  }
}

export default function ChatPage() {
  const [conversationId, setConversationId] = useState('')
  const [messages, setMessages] = useState<Message[]>([])
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const [error, setError] = useState('')
  const [drafts, setDrafts] = useState<Record<string, string>>({})

  useEffect(() => {
    api
      .createConversation('新对话')
      .then((item) => setConversationId(item.id))
      .catch((reason: Error) => setError(reason.message))
  }, [])

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
      const conversation = await api.getConversation(conversationId)
      setMessages(conversation.messages)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '发送失败')
    } finally {
      setSending(false)
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
                {message.content.type === 'component_candidate' && (
                  <span className="effect-label">
                    候选组件：{message.content.componentName} · 待补参数{' '}
                    {(message.content.required ?? []).join(', ') || '无'}
                  </span>
                )}
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
