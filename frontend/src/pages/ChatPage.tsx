import Form from '@rjsf/core'
import validator from '@rjsf/validator-ajv8'
import { Bot, CornerDownLeft, Link2Off, MessageSquareText } from 'lucide-react'
import { FormEvent, useCallback, useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'

import {
  api,
  ChatMessageContent,
  conversationRunUrl,
  ConversationRunEvent,
  type ConversationRunSummary,
  type Run,
  taskCheckpointUrl,
  type TaskCheckpointSummary,
} from '@/api/client'

type Message = {
  id: string
  role: string
  content: ChatMessageContent
}

export default function ChatPage() {
  const { id: routeConversationId } = useParams()
  const navigate = useNavigate()
  const [conversationId, setConversationId] = useState(routeConversationId ?? '')
  const [messages, setMessages] = useState<Message[]>([])
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const [error, setError] = useState('')
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  // 候选卡片补参状态，按消息 id 索引。
  const [candidateInputs, setCandidateInputs] = useState<Record<string, Record<string, unknown>>>({})
  const [confirming, setConfirming] = useState('')
  const [startingAgent, setStartingAgent] = useState(false)
  // 轨迹抽屉：追踪最近一次通用 Agent 运行及其规范化事件。
  const [activeRunId, setActiveRunId] = useState('')
  const [runStatus, setRunStatus] = useState('')
  const [traceEvents, setTraceEvents] = useState<ConversationRunEvent[]>([])
  // 每个 ConversationRun 的实时终态，供展示层判断“只有 succeeded 可固化”。
  const [agentRuns, setAgentRuns] = useState<Record<string, ConversationRunSummary>>({})
  const [taskCheckpoints, setTaskCheckpoints] = useState<Record<string, TaskCheckpointSummary>>(
    {},
  )
  const [activeCheckpointIds, setActiveCheckpointIds] = useState('')
  const [workflowRuns, setWorkflowRuns] = useState<Record<string, Run>>({})
  const [activeWorkflowRunIds, setActiveWorkflowRunIds] = useState('')

  // conversationId 写入路由：新对话创建后导航到 /chat/:id，刷新可恢复同一会话。
  useEffect(() => {
    if (routeConversationId) {
      setConversationId(routeConversationId)
      return
    }
    api
      .createConversation('新对话')
      .then((item) => navigate(`/chat/${item.id}`, { replace: true }))
      .catch((reason: Error) => setError(reason.message))
  }, [routeConversationId, navigate])

  const refresh = useCallback(async () => {
    if (!conversationId) return
    const conversation = await api.getConversation(conversationId)
    setMessages(conversation.messages)
    const agentRunIds = Array.from(
      new Set(
        conversation.messages
          .filter(
            (message) =>
              message.content.conversationRunId &&
              (message.content.type === 'agent_run' || message.content.runType === 'agent_run'),
          )
          .map((message) => message.content.conversationRunId as string),
      ),
    )
    const workflowRunIds = Array.from(
      new Set(
        conversation.messages
          .filter((message) => message.content.runId)
          .map((message) => message.content.runId as string),
      ),
    )
    const [agentSummaries, runSummaries] = await Promise.all([
      Promise.all(agentRunIds.map((runId) => api.getConversationRun(runId).catch(() => null))),
      Promise.all(workflowRunIds.map((runId) => api.getRun(runId).catch(() => null))),
    ])
    const checkpointIds = new Set(
      conversation.messages
        .map((message) => message.content.taskCheckpointId)
        .filter((id): id is string => typeof id === 'string'),
    )
    for (const summary of agentSummaries) {
      const checkpointId = summary?.error?.taskCheckpointId
      if (typeof checkpointId === 'string') checkpointIds.add(checkpointId)
    }
    const checkpointSummaries = await Promise.all(
      Array.from(checkpointIds).map((checkpointId) =>
        api.getTaskCheckpoint(checkpointId).catch(() => null),
      ),
    )
    setAgentRuns(
      Object.fromEntries(
        agentSummaries
          .filter((summary): summary is ConversationRunSummary => summary !== null)
          .map((summary) => [summary.id, summary]),
      ),
    )
    const loadedCheckpoints = checkpointSummaries.filter(
      (checkpoint): checkpoint is TaskCheckpointSummary => checkpoint !== null,
    )
    setTaskCheckpoints(
      Object.fromEntries(loadedCheckpoints.map((checkpoint) => [checkpoint.id, checkpoint])),
    )
    setActiveCheckpointIds(
      loadedCheckpoints
        .filter((checkpoint) => checkpoint.chainStatus === 'continuing')
        .map((checkpoint) => checkpoint.id)
        .sort()
        .join(','),
    )
    const loadedRuns = runSummaries.filter((run): run is Run => run !== null)
    setWorkflowRuns(Object.fromEntries(loadedRuns.map((run) => [run.id, run])))
    setActiveWorkflowRunIds(
      loadedRuns
        .filter((run) => run.status === 'queued' || run.status === 'running')
        .map((run) => run.id)
        .join(','),
    )
    if (agentRunIds.length > 0) {
      // 从服务端消息恢复最近一次 Agent 运行，刷新后仍可展示轨迹，不依赖本地 state。
      setActiveRunId(agentRunIds[agentRunIds.length - 1])
    }
  }, [conversationId])

  useEffect(() => {
    refresh().catch((reason: Error) => setError(reason.message))
  }, [refresh])

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
        setAgentRuns((current) => ({ ...current, [summary.id]: summary }))
        setTraceEvents(events)
        // queued/running 均为非终态：长任务由独立 Worker 执行，持续轮询直到终态。
        if (summary.status === 'queued' || summary.status === 'running') {
          window.setTimeout(poll, 800)
        } else if (
          summary.status === 'failed' &&
          typeof summary.error?.taskCheckpointId === 'string'
        ) {
          await refresh()
        }
      } catch (reason) {
        if (!cancelled) setError(reason instanceof Error ? reason.message : '加载轨迹失败')
      }
    }
    poll()
    return () => {
      cancelled = true
    }
  }, [activeRunId, refresh])

  // source failed 已是 AgentRun 终态，后续改由持久 checkpoint 链驱动 Conversation 刷新。
  useEffect(() => {
    if (!activeCheckpointIds) return
    let cancelled = false
    let timer = 0
    let attempts = 0
    async function poll() {
      try {
        await refresh()
        if (cancelled) return
        attempts += 1
        if (attempts >= 375) {
          setError('失败接力状态刷新超时；服务端仍会继续，可刷新页面恢复跟踪。')
          return
        }
        timer = window.setTimeout(poll, 800)
      } catch (reason) {
        if (!cancelled) {
          setError(reason instanceof Error ? reason.message : '加载失败接力状态失败')
          attempts += 1
          if (attempts < 375) timer = window.setTimeout(poll, 800)
        }
      }
    }
    timer = window.setTimeout(poll, 800)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [activeCheckpointIds, refresh])

  useEffect(() => {
    if (!activeWorkflowRunIds) return
    let cancelled = false
    const runIds = activeWorkflowRunIds.split(',').filter(Boolean)
    async function poll() {
      try {
        const runs = await Promise.all(runIds.map((runId) => api.getRun(runId)))
        if (cancelled) return
        setWorkflowRuns((current) => ({
          ...current,
          ...Object.fromEntries(runs.map((run) => [run.id, run])),
        }))
        const pending = runs.filter(
          (run) => run.status === 'queued' || run.status === 'running',
        )
        if (pending.length > 0) window.setTimeout(poll, 800)
      } catch (reason) {
        if (!cancelled) setError(reason instanceof Error ? reason.message : '加载运行结果失败')
      }
    }
    poll()
    return () => {
      cancelled = true
    }
  }, [activeWorkflowRunIds])

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
      const result = await api.sendMessage(conversationId, text)
      if (
        result.assistant.conversationRunId &&
        (result.assistant.type === 'agent_run' || result.assistant.runType === 'agent_run')
      ) {
        setActiveRunId(result.assistant.conversationRunId)
      }
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

  async function cancelActiveRun() {
    if (!activeRunId) return
    try {
      await api.cancelConversationRun(activeRunId)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '取消失败')
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

  // 候选卡片绑定产生它的用户消息：取该助手消息之前最近的一条用户消息文本，
  // 而不是全局最近 prompt，避免继续 Agent 时误用无关的新输入。
  function sourcePromptFor(message: Message): string {
    const index = messages.findIndex((item) => item.id === message.id)
    for (let cursor = index - 1; cursor >= 0; cursor -= 1) {
      if (messages[cursor].role === 'user') {
        return messages[cursor].content.text ?? ''
      }
    }
    return ''
  }

  function renderCandidate(message: Message) {
    const content = message.content
    const sourcePrompt = sourcePromptFor(message)
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
                disabled={startingAgent || !sourcePrompt}
                onClick={() => startAgent(sourcePrompt)}
              >
                {startingAgent ? '正在启动 Agent...' : '继续通用 Agent'}
              </button>
            </div>
          </Form>
        )}
      </div>
    )
  }

  function renderWorkflowCandidates(message: Message) {
    return (
      <div className="candidate-card">
        <div className="candidate-meta">
          <strong>Workflow 候选</strong>
          <span className="effect-label">已加载 {message.content.level ?? 'L0'}</span>
        </div>
        <div className="node-table">
          {(message.content.items ?? []).map((item) => (
            <div className="node-row" key={item.workflowRef}>
              <div>
                <strong>{item.name}</strong>
                <small>{item.summary}</small>
                <div className="current-mapping">
                  <span className="metric-chip">
                    匹配 {(item.matchReason.score * 100).toFixed(0)}%
                  </span>
                  <span className="metric-chip">必填 {item.inputSynopsis.required.join(', ') || '无'}</span>
                  <span className="metric-chip">副作用 {item.sideEffects}</span>
                  <span className="metric-chip">LLM {item.estimatedCost.llmCalls}</span>
                  {item.approvalRequired && <span className="effect-label">需要审批</span>}
                </div>
                <code className="digest-line">{item.workflowRef}</code>
              </div>
            </div>
          ))}
        </div>
      </div>
    )
  }

  function renderContinuationLineage(checkpoint: TaskCheckpointSummary) {
    return (
      <div className="current-mapping" aria-label="失败接力 lineage">
        {checkpoint.lineage.map((item) => (
          <span className="metric-chip" key={item.checkpointId}>
            {item.generation > 0 ? `第 ${item.generation} 代` : '等待接力'} · {item.status}
            {' · '}
            <a
              href={taskCheckpointUrl(item.checkpointId)}
              target="_blank"
              rel="noreferrer"
            >
              checkpoint {item.checkpointId.slice(0, 8)}
            </a>
            {item.continuationRunId && (
              <>
                {' · '}
                <a
                  href={conversationRunUrl(item.continuationRunId)}
                  target="_blank"
                  rel="noreferrer"
                >
                  run {item.continuationRunId.slice(0, 8)}
                </a>
              </>
            )}
          </span>
        ))}
      </div>
    )
  }

  // succeeded 才可固化；可恢复失败以 checkpoint 链终态为准，不提前显示源错误。
  function renderAgentRunActions(conversationRunId: string, messageCheckpointId?: string) {
    const run = agentRuns[conversationRunId]
    const status = run?.status
    const errorCheckpointId =
      typeof run?.error?.taskCheckpointId === 'string'
        ? run.error.taskCheckpointId
        : undefined
    const checkpoint = taskCheckpoints[errorCheckpointId ?? messageCheckpointId ?? '']
    const terminalOwnerRunId = checkpoint?.lineage.at(-1)?.sourceRunId
    if (drafts[conversationRunId]) {
      return (
        <Link className="button ghost" to={`/workflows/${drafts[conversationRunId]}/edit`}>
          打开固化草稿
        </Link>
      )
    }
    if (status === 'succeeded') {
      return (
        <>
          {run.output?.reply && (
            <div className="result-panel" role="status">
              {String(run.output.reply)}
            </div>
          )}
          {checkpoint && renderContinuationLineage(checkpoint)}
          <button className="button ghost" onClick={() => distill(conversationRunId)}>
            固化为组件
          </button>
        </>
      )
    }
    if (checkpoint?.chainStatus === 'continuing') {
      return (
        <div className="result-panel" role="status">
          <strong>{checkpoint.continuationRunId ? '接力中' : '等待接力'}</strong>
          <span>源失败证据已保留，Worker 将从冻结的剩余计划继续。</span>
          {renderContinuationLineage(checkpoint)}
        </div>
      )
    }
    if (checkpoint?.chainStatus === 'succeeded') {
      return (
        <div className="hint" role="status">
          源运行失败；接力链已成功完成，最终结果见后续消息。
          {renderContinuationLineage(checkpoint)}
        </div>
      )
    }
    if (checkpoint?.chainStatus === 'awaiting_user') {
      if (terminalOwnerRunId !== conversationRunId) {
        return (
          <div className="hint" role="status">
            本代接力未完成；接力链末端正在等待人工确认。
            {renderContinuationLineage(checkpoint)}
          </div>
        )
      }
      return (
        <div className="error-panel" role="alert">
          <strong>{checkpoint.chainError?.code ?? 'CONTINUATION_AWAITING_USER'}</strong>
          <span>{checkpoint.chainError?.message ?? checkpoint.pendingQuestion}</span>
          {renderContinuationLineage(checkpoint)}
        </div>
      )
    }
    if (checkpoint && ['failed', 'cancelled'].includes(checkpoint.chainStatus)) {
      if (terminalOwnerRunId !== conversationRunId) {
        return (
          <div className="hint" role="status">
            本代接力未完成；最终终态见接力链末端消息。
            {renderContinuationLineage(checkpoint)}
          </div>
        )
      }
      return (
        <div className="error-panel" role="alert">
          <strong>{checkpoint.chainError?.code ?? 'CONTINUATION_FAILED'}</strong>
          <span>{checkpoint.chainError?.message ?? '失败接力未能完成'}</span>
          {run?.error && (
            <span>
              源错误：{String(run.error.code ?? status)} ·{' '}
              {String(run.error.message ?? '运行未完成')}
            </span>
          )}
          {renderContinuationLineage(checkpoint)}
        </div>
      )
    }
    if (status === 'failed' && errorCheckpointId && !checkpoint) {
      return (
        <div className="result-panel" role="status">
          <strong>正在确认失败接力</strong>
          <span>源失败证据已保存，正在读取 checkpoint 链状态。</span>
        </div>
      )
    }
    if ((status === 'failed' || status === 'cancelled') && run.error) {
      return (
        <div className="error-panel" role="alert">
          <strong>{String(run.error.code ?? status)}</strong>
          <span>{String(run.error.message ?? '运行未完成')}</span>
        </div>
      )
    }
    return (
      <span className="hint">
        {status ? `运行状态 ${status}，仅 succeeded 可固化` : '运行未完成，暂不可固化'}
      </span>
    )
  }

  function renderWorkflowRun(runId: string) {
    const run = workflowRuns[runId]
    if (!run) return <span className="hint">正在读取运行状态...</span>
    if (run.status === 'queued' || run.status === 'running') {
      return <span className={`status-badge ${run.status}`}>{run.status}</span>
    }
    if (run.status === 'succeeded') {
      const reply =
        run.output && typeof run.output.reply === 'string' ? run.output.reply : undefined
      return (
        <div className="result-panel" role="status" aria-label="Workflow 运行结果">
          {reply ?? JSON.stringify(run.output)}
        </div>
      )
    }
    const code = run.error?.code ?? run.status
    const message = run.error?.message ?? '运行未完成'
    return (
      <div className="error-panel" role="alert" aria-label="Workflow 运行错误">
        <strong>{String(code)}</strong>
        <span>{String(message)}</span>
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
                {message.content.disclosureSessionId && (
                  <div className="current-mapping">
                    <span className="metric-chip">
                      Disclosure {message.content.disclosureLevel ?? 'L0'}
                    </span>
                    {message.content.agentAction && (
                      <span className="metric-chip">Action {message.content.agentAction}</span>
                    )}
                    {message.content.selectionReason && (
                      <span className="candidate-reason">
                        选择依据：{message.content.selectionReason}
                      </span>
                    )}
                    <code className="digest-line">
                      session {message.content.disclosureSessionId.slice(0, 8)}
                    </code>
                    {message.content.pendingSelectionId && (
                      <code className="digest-line">
                        pending {message.content.pendingSelectionId.slice(0, 8)}
                      </code>
                    )}
                    {message.content.resumedPendingSelection && (
                      <span className="effect-label">已恢复原选择</span>
                    )}
                  </div>
                )}
                {(message.content.toolTimeline?.length ?? 0) > 0 && (
                  <ol className="trace-timeline" aria-label="Workflow Broker 时间线">
                    {message.content.toolTimeline?.map((item, index) => (
                      <li className="trace-item" key={`${item.callId}-${item.type}-${index}`}>
                        <span className="trace-type">{item.type}</span>
                        <span className="metric-chip">{item.level ?? 'L0'}</span>
                        {item.capabilityRef && (
                          <span className="trace-tool">{item.capabilityRef}</span>
                        )}
                        {item.callId && <code className="digest-line">{item.callId}</code>}
                        {item.status && <span className="metric-chip">{item.status}</span>}
                        {(item.diagnostics ?? []).map((diagnostic) => (
                          <div
                            className={`diagnostic ${diagnostic.level}`}
                            key={`${diagnostic.code}-${diagnostic.message}`}
                          >
                            <strong>{diagnostic.code}</strong>
                            <span>{diagnostic.message}</span>
                          </div>
                        ))}
                      </li>
                    ))}
                  </ol>
                )}
                {message.content.type === 'component_candidate' && renderCandidate(message)}
                {message.content.type === 'workflow_candidates' &&
                  renderWorkflowCandidates(message)}
                {message.content.offerAgentRun && (
                  <button
                    className="button ghost"
                    type="button"
                    disabled={startingAgent}
                    onClick={() => startAgent(sourcePromptFor(message))}
                  >
                    {startingAgent ? '正在启动 Agent...' : '启动通用 Agent 探索'}
                  </button>
                )}
                {message.content.runId && (
                  <>
                    {renderWorkflowRun(message.content.runId)}
                    <Link className="button ghost" to={`/runs/${message.content.runId}`}>
                      查看运行
                    </Link>
                  </>
                )}
                {message.content.conversationRunId &&
                  (message.content.type === 'agent_run' ||
                    message.content.runType === 'agent_run') &&
                  renderAgentRunActions(
                    message.content.conversationRunId,
                    message.content.taskCheckpointId,
                  )}
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
            {(runStatus === 'queued' || runStatus === 'running') && (
              <button className="button ghost" type="button" onClick={cancelActiveRun}>
                取消运行
              </button>
            )}
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
