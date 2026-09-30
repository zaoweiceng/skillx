import MarkdownContent from '@/components/MarkdownContent'
import { TaskTrace } from '@/features/chat/TaskTrace'
import { LLMTrace } from '@/components/LLMTrace'
import TaskPanel from '@/components/TaskPanel'
import { useState } from 'react'
import { Button, Tag } from 'antd'
import Form from '@/components/SchemaForm'
import validator from '@rjsf/validator-ajv8'
import {
  Bot,
  History,
  PanelLeftClose,
  PanelLeftOpen,
  PanelRightClose,
  MessageSquareText,
  Plus,
  Workflow,
} from 'lucide-react'
import { Link } from 'react-router-dom'
import { conversationRunUrl, taskCheckpointUrl, type TaskCheckpointSummary } from '@/api/client'
import { useConversation, type Message } from '@/features/chat/useConversation'
import { rjsfSchema } from '@/lib/utils'
import styles from '@/features/chat/chat.module.css'
import { ConversationHistoryItem } from '@/features/chat/ConversationHistoryItem'
import { ChatComposer } from '@/features/chat/ChatComposer'
import { ChatImageAttachments } from '@/features/chat/ChatImageAttachments'
import { useStoredBoolean } from '@/hooks/useStoredBoolean'
import { ChatWorkspace } from '@/features/chat/ChatWorkspace'
import { AgentSelector } from '@/features/runtime/AgentSelector'

export default function ChatPage() {
  const [historyCollapsed, setHistoryCollapsed] = useStoredBoolean('skillx-history-collapsed')
  const [runtimePending, setRuntimePending] = useState(false)
  const { refresh, workingDirectory, changeWorkingDirectory, draftSelection, setDraftSelection, renameConversation, deleteConversation, turnFor, elapsedLabel, tokenLabel, conversationId, messages, history, historyCursor, historyOpen, setHistoryOpen, traceOpen, setTraceOpen, input, setInput, sending, error, copyFeedback, drafts, candidateInputs, setCandidateInputs, confirming, startingAgent, activeRunId, runStatus, traceEvents, agentRuns, taskCheckpoints, workflowRuns, copyResult, send, createNewConversation, loadMoreHistory, toggleWorkflowMode, startComponent, startAgent, cancelActiveRun, distill } = useConversation()

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
            schema={rjsfSchema(content.inputSchema)}
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
              <Button
                className="button primary"
                htmlType="submit"
                disabled={confirming === message.id || !content.candidateSnapshotDigest}
              >
                {confirming === message.id ? '正在启动...' : '使用该组件'}
              </Button>
              <Button
                className="button ghost"
                htmlType="button"
                disabled={startingAgent || !sourcePrompt}
                onClick={() => startAgent(sourcePrompt)}
              >
                {startingAgent ? '正在启动 Agent...' : '继续通用 Agent'}
              </Button>
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
    const blockedDiagnostics =
      run?.distillation?.status === 'blocked'
        ? (run.distillation.analysis.diagnostics ?? []).filter(
            (item) => item.level === 'error',
          )
        : []
    const blockedDiagnosticList = blockedDiagnostics.length > 0 && (
      <ul className="diagnostic-list" aria-label="固化阻断诊断">
        {blockedDiagnostics.map((diagnostic) => (
          <li key={`${diagnostic.code}-${diagnostic.message}`}>
            <strong>{diagnostic.code}</strong>：{diagnostic.message}
          </li>
        ))}
      </ul>
    )
    const persistedDraftId = drafts[conversationRunId] ?? run?.distillation?.draftWorkflowId
    if (persistedDraftId) {
      return (
        <>
          {blockedDiagnosticList}
          <Link className="button ghost" to={`/workflows/${persistedDraftId}/edit`}>
            打开固化草稿
          </Link>
        </>
      )
    }
    if (status === 'succeeded') {
      const eligibility = run.distillationEligibility
      const ineligibleReason =
        eligibility?.reasonCode === 'NO_REUSABLE_ACTIONS'
          ? '本次没有可复用动作'
          : eligibility?.reasonCode === 'DRAFT_ALREADY_EXISTS'
            ? '本次运行已有固化草稿'
            : '本次运行不可固化'
      return (
        <>
          {run.output?.reply && (
            <div className="result-panel" role="status">
              {String(run.output.reply)}
            </div>
          )}
          {checkpoint && renderContinuationLineage(checkpoint)}
          {blockedDiagnosticList}
          {eligibility?.eligible ? (
            <Button className="button ghost" onClick={() => distill(conversationRunId)}>
              固化为组件
            </Button>
          ) : (
            <span className="hint">{ineligibleReason}</span>
          )}
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
      return (
        <div className="dual-result" role="status" aria-label="Workflow 运行结果">
          <div className="result-layer" aria-label="结构化结果">
            <div className="result-layer-heading">
              <strong>结构化结果</strong>
              <Button
                className="button ghost"
                htmlType="button"
                onClick={() => void copyResult(JSON.stringify(run.output, null, 2), 'json')}
              >
                {copyFeedback === 'json:ok'
                  ? '已复制 JSON'
                  : copyFeedback === 'json:failed'
                    ? '复制失败'
                    : '复制 JSON'}
              </Button>
            </div>
            {typeof run.output?.reply === 'string' && (
              <p className="structured-summary">{run.output.reply}</p>
            )}
            <pre className="json-block">{JSON.stringify(run.output, null, 2)}</pre>
          </div>
          {run.presentation?.status === 'completed' && (
            <div className="result-layer" aria-label="阅读结果">
              <div className="result-layer-heading">
                <strong>阅读结果</strong>
                <Button
                  className="button ghost"
                  htmlType="button"
                  onClick={() => void copyResult(run.presentation?.text ?? '', 'text')}
                >
                  {copyFeedback === 'text:ok'
                    ? '已复制文本'
                    : copyFeedback === 'text:failed'
                      ? '复制失败'
                      : '复制文本'}
                </Button>
              </div>
              <p className="presentation-text">{run.presentation.text}</p>
            </div>
          )}
          {run.presentation?.status === 'running' && (
            <span className="hint">正在整理阅读结果...</span>
          )}
          {run.presentation?.status === 'failed' && (
            <span className="error-code">文本整理失败，结构化结果不受影响</span>
          )}
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
    const taskId = [...messages].reverse().find(message => message.content.taskId)?.content.taskId
    if (taskId) return traceOpen ? <TaskTrace key={taskId} id={taskId} /> : null
    if (!activeRunId || traceEvents.length === 0) {
      return (
        <div className="trace-empty">
          <span className="pulse-ring" />
          <p>Agent 启动后，这里只展示可审计的行动摘要与 Tool 事件。</p>
        </div>
      )
    }
    return (
      <><LLMTrace events={traceEvents} /><ol className="trace-timeline" aria-label="Agent 轨迹">
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
      </ol></>
    )
  }

  return (
    <div className={`chat-page ${styles.page}`}>
      <header className="chat-header">
        <div>
          {historyCollapsed && <Button type="text" aria-label="展开历史对话" aria-expanded={false}
            onClick={() => { setHistoryCollapsed(false); setHistoryOpen(true) }} icon={<PanelLeftOpen size={17} />} />}
          <span className="eyebrow">工作空间 / 对话</span>
          <h1>通用对话</h1>
        </div>
        <div className="chat-header-actions">
          <Button
            className="mobile-history-toggle"
            htmlType="button"
            aria-label="打开历史对话"
            aria-expanded={historyOpen}
            onClick={() => { setHistoryCollapsed(false); setHistoryOpen((current) => !current) }}
          >
            <History size={14} />
          </Button>
          <Button
            className="mobile-history-toggle"
            htmlType="button"
            aria-label="打开执行轨迹"
            title={traceOpen ? '收起执行轨迹' : '打开执行轨迹'}
            aria-controls="execution-trace-sidebar"
            aria-expanded={traceOpen}
            onClick={() => setTraceOpen((current) => !current)}
          >
            <Bot size={14} />
          </Button>
        </div>
      </header>
      <ChatWorkspace collapsed={historyCollapsed} traceOpen={traceOpen} trace={
        <aside id="execution-trace-sidebar" className="trace-sidebar" aria-label="执行轨迹" hidden={!traceOpen}>
          <div className="panel-heading">
            <div>
              <span className="eyebrow">EXECUTION TRACE</span>
              <h2>执行轨迹</h2>
            </div>
            <Button type="text" aria-label="收起执行轨迹" title="收起执行轨迹"
              onClick={() => setTraceOpen(false)} icon={<PanelRightClose size={17} />} />
            {runStatus && <span className={`status-badge ${runStatus}`}>{runStatus}</span>}
            {(runStatus === 'queued' || runStatus === 'running') && (
              <Button className="button ghost" htmlType="button" onClick={cancelActiveRun}>
                取消运行
              </Button>
            )}
          </div>
          {renderTrace()}
        </aside>
      } sidebar={
        <aside
          className={`conversation-history ${historyOpen ? 'open' : ''}`}
          aria-label="历史对话"
        >
          <div className="history-heading">
            <span><History size={14} /> 历史对话</span>
            <div className="history-heading-actions">
            <Button type="text" aria-label="折叠历史对话" aria-expanded={true}
              onClick={() => { setHistoryCollapsed(true); setHistoryOpen(false) }} icon={<PanelLeftClose size={16} />} />
            <Button htmlType="button" aria-label="新建对话" onClick={createNewConversation}>
              <Plus size={15} />
            </Button>
            </div>
          </div>
          <nav>
            {history.map((item) => (
              <ConversationHistoryItem key={item.id} id={item.id} title={item.title}
                active={item.id === conversationId} onNavigate={() => setHistoryOpen(false)}
                onRename={renameConversation} onDelete={deleteConversation} />
            ))}
            {history.length === 0 && <span className="hint">暂无历史对话</span>}
          </nav>
          {historyCursor && (
            <Button className="button ghost" htmlType="button" onClick={loadMoreHistory}>
              加载更多
            </Button>
          )}
        </aside>}>
        <section className="message-column">
          <div className="message-stream">
          {messages.length === 0 && (
            <div className="chat-empty">
              <span className="chat-empty-icon">
                <MessageSquareText size={28} />
              </span>
              <span className={styles.welcomeLabel}>想法，从这里开始</span>
              <h2>今天，一起完成什么？</h2>
              <p>
                描述你的目标，一起完成任务，并将重复步骤保存为工作流。
              </p>
              <div className="prompt-suggestions">
                <Button onClick={() => setInput('帮我梳理一个重复任务的执行步骤')}>
                  <Workflow size={20} /><strong>梳理重复任务</strong><span>把日常步骤变成自动化流程</span>
                </Button>
                <Button onClick={() => setInput('分析现有组件是否适合我的任务')}>
                  <Bot size={20} /><strong>匹配已有组件</strong><span>发现工作空间中的可用能力</span>
                </Button>
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
                {message.content.images && <ChatImageAttachments images={message.content.images} />}
                {!message.content.taskId && (message.role === 'assistant' ? <MarkdownContent>{message.content.text ?? ''}</MarkdownContent> : <p>{message.content.text}</p>)}
                {!!message.content.referenceFiles?.length && <div className={styles.attachments}>
                  {message.content.referenceFiles.map((file, index) => <Tag key={`${file.name}-${index}`}>{file.name}</Tag>)}
                </div>}
                {message.role === 'assistant' && turnFor(message) && (
                  <div className="turn-metrics">
                    <span className="metric-chip">
                      {turnFor(message)?.workflowModeSnapshot === 'enabled'
                        ? '工作流开启'
                        : '工作流关闭'}
                    </span>
                    <span className="metric-chip">{elapsedLabel(turnFor(message))}</span>
                    <span className="metric-chip">{tokenLabel(turnFor(message))}</span>
                  </div>
                )}
                {message.content.code && (
                  <span className="error-code">{message.content.code}</span>
                )}
                {(message.content.diagnostics?.length ?? 0) > 0 && (
                  <ul className="diagnostic-list" aria-label="失败诊断">
                    {message.content.diagnostics?.map((diagnostic) => (
                      <li key={`${diagnostic.field ?? ''}-${diagnostic.code}`}>
                        <strong>{diagnostic.code}</strong>
                        {diagnostic.field ? ` · ${diagnostic.field}` : ''}：{diagnostic.message}
                      </li>
                    ))}
                  </ul>
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
                {(message.content.bindingSources?.length ?? 0) > 0 && (
                  <div className="binding-evidence" aria-label="参数绑定来源">
                    {message.content.bindingSources?.map((source) => (
                      <span className="metric-chip" key={`${source.field}-${source.quote}`}>
                        {source.field} ← “{source.quote}”
                        {' · '}
                        {(source.confidence * 100).toFixed(0)}%
                        {source.serverVerified
                          ? ` · 服务端已校验${source.normalizationType ? ` (${source.normalizationType})` : ''}`
                          : ' · 待确认'}
                      </span>
                    ))}
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
                        {item.action && <span className="metric-chip">{item.action}</span>}
                        {item.attempt && (
                          <span className="metric-chip">第 {item.attempt} 次</span>
                        )}
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
                {message.content.taskId && <TaskPanel id={message.content.taskId} onFinished={refresh} />}
                {message.content.type === 'component_candidate' && renderCandidate(message)}
                {message.content.type === 'workflow_candidates' &&
                  renderWorkflowCandidates(message)}
                {message.content.offerAgentRun &&
                  message.content.code !== 'WORKFLOW_SELECTION_UNAVAILABLE' && (
                  <Button
                    className="button ghost"
                    htmlType="button"
                    disabled={startingAgent}
                    onClick={() => startAgent(sourcePromptFor(message))}
                  >
                    {startingAgent ? '正在启动 Agent...' : '启动通用 Agent 探索'}
                  </Button>
                )}
                {message.content.code === 'WORKFLOW_SELECTION_UNAVAILABLE' && (
                  <>
                    <Link className="button ghost" to="/settings">
                      前往运行时设置并探测
                    </Link>
                    <Button className="button ghost" htmlType="button" onClick={toggleWorkflowMode}>
                      关闭工作流，下一条交给通用 Agent
                    </Button>
                  </>
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
          </div>
          <ChatComposer key={conversationId} input={input} onInputChange={setInput}
            sending={sending} disabled={sending || runtimePending} onSend={send}
            workingDirectory={workingDirectory} onWorkingDirectoryChange={changeWorkingDirectory}
            controls={<AgentSelector key={conversationId} conversationId={conversationId} draftSelection={draftSelection} onDraftChange={setDraftSelection} onBusyChange={setRuntimePending} />} />
        </section>

      </ChatWorkspace>
      {error && <div className="chat-error">{error}</div>}
    </div>
  )
}
