import { FormEvent, useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { runtimeRequest } from '@/features/runtime/api'
import { readSelectionPreference } from '@/features/runtime/selectionPreference'
import type { Selection } from '@/features/runtime/AgentSelector'

import {
  api,
  ChatMessageContent,
  type ChatImageInput,
  type ChatReferenceFile,
  ConversationRunEvent,
  type ConversationRunSummary,
  type ConversationSummary,
  type ConversationTurn,
  type Run,
  type TaskCheckpointSummary,
} from '@/api/client'

const tokenNumber = new Intl.NumberFormat('en', {
  notation: 'compact', maximumFractionDigits: 1,
})

export type Message = {
  id: string
  role: string
  content: ChatMessageContent
  turnId?: string | null
}

export function useConversation() {
  const { id: routeConversationId } = useParams()
  const navigate = useNavigate()
  const sendingRef = useRef(false)
  const pendingConversationRef = useRef('')
  const [draftSelection, setDraftSelection] = useState<Selection>(readSelectionPreference)
  const [workingDirectory, setWorkingDirectory] = useState<string>()
  const [conversationId, setConversationId] = useState(routeConversationId ?? '')
  const [messages, setMessages] = useState<Message[]>([])
  const [turns, setTurns] = useState<ConversationTurn[]>([])
  const [history, setHistory] = useState<ConversationSummary[]>([])
  const [historyCursor, setHistoryCursor] = useState<string | null>(null)
  const [historyOpen, setHistoryOpen] = useState(false)
  const [traceOpen, setTraceOpen] = useState(false)
  const [workflowMode, setWorkflowMode] = useState<'enabled' | 'disabled'>('enabled')
  const [workflowSelectionAvailable, setWorkflowSelectionAvailable] = useState<
    boolean | null
  >(null)
  const [switchingMode, setSwitchingMode] = useState(false)
  const [clock, setClock] = useState(Date.now())
  const [optimisticStartedAt, setOptimisticStartedAt] = useState<number | null>(null)
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const [error, setError] = useState('')
  const [copyFeedback, setCopyFeedback] = useState('')
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

  async function copyResult(value: string, kind: 'json' | 'text') {
    try {
      await navigator.clipboard.writeText(value)
      setCopyFeedback(`${kind}:ok`)
    } catch {
      setCopyFeedback(`${kind}:failed`)
    }
  }

  useEffect(() => {
    setConversationId(routeConversationId ?? '')
    if (!routeConversationId) setDraftSelection(readSelectionPreference())
    pendingConversationRef.current = ''
    setWorkingDirectory(undefined)
    setInput('')
    setError('')
  }, [routeConversationId])

  useEffect(() => {
    api
      .getHealth()
      .then((health) => {
        setWorkflowSelectionAvailable(health.workflowSelectionMode === 'real')
      })
      .catch(() => setWorkflowSelectionAvailable(null))
  }, [])

  const refresh = useCallback(async () => {
    if (!conversationId) return
    const conversation = await api.getConversation(conversationId)
    setWorkingDirectory(conversation.workingDirectory ?? undefined)
    setMessages(conversation.messages)
    setTurns(conversation.turns ?? [])
    setWorkflowMode(conversation.workflowMode ?? 'enabled')
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
        .filter(
          (run) =>
            run.status === 'queued' ||
            run.status === 'running' ||
            (run.presentationExpected &&
              run.presentation?.status !== 'completed' &&
              run.presentation?.status !== 'failed'),
        )
        .map((run) => run.id)
        .join(','),
    )
    if (agentRunIds.length > 0) {
      // 从服务端消息恢复最近一次 Agent 运行，刷新后仍可展示轨迹，不依赖本地 state。
      setActiveRunId(agentRunIds[agentRunIds.length - 1])
    }
  }, [conversationId])

  const refreshHistory = useCallback(async () => {
    if (typeof api.listConversations !== 'function') return
    const page = await api.listConversations()
    setHistory(page.items)
    setHistoryCursor(page.nextCursor)
  }, [])

  useEffect(() => {
    refresh().catch((reason: Error) => setError(reason.message))
    refreshHistory().catch((reason: Error) => setError(reason.message))
  }, [refresh, refreshHistory])

  useEffect(() => {
    setMessages([])
    setTurns([])
    setTraceEvents([])
    setAgentRuns({})
    setWorkflowRuns({})
    setTaskCheckpoints({})
    setActiveRunId('')
    setOptimisticStartedAt(null)
  }, [conversationId])

  useEffect(() => {
    const hasActiveTurn = turns.some((turn) => !turn.completedAt)
    if (!hasActiveTurn && optimisticStartedAt === null) return
    const timer = window.setInterval(() => setClock(Date.now()), 250)
    return () => window.clearInterval(timer)
  }, [turns, optimisticStartedAt])

  const activeTaskTurns = turns.filter(turn => !turn.completedAt && messages.some(message =>
    message.turnId === turn.id && message.content.taskId,
  )).map(turn => turn.id).sort().join(',')
  useEffect(() => {
    if (!activeTaskTurns) return
    let cancelled = false
    let timer = 0
    async function poll() {
      try { await refresh() }
      catch (reason) { if (!cancelled) setError(reason instanceof Error ? reason.message : '用量刷新失败') }
      if (!cancelled) timer = window.setTimeout(poll, 1500)
    }
    timer = window.setTimeout(poll, 1500)
    return () => { cancelled = true; window.clearTimeout(timer) }
  }, [activeTaskTurns, refresh])

  // 订阅活动 Agent 运行的规范化事件，直到进入终态。
  useEffect(() => {
    if (!activeRunId) return
    let cancelled = false
    let timer = 0
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
          timer = window.setTimeout(poll, 800)
        } else {
          // 终态提交与 Usage 账本在服务端事务完成后，强制刷新一次 Turn。
          await refresh()
        }
      } catch (reason) {
        if (!cancelled) setError(reason instanceof Error ? reason.message : '加载轨迹失败')
      }
    }
    poll()
    return () => {
      cancelled = true
      window.clearTimeout(timer)
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
    let timer = 0
    let attempts = 0
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
          (run) =>
            run.status === 'queued' ||
            run.status === 'running' ||
            (run.presentationExpected &&
              run.presentation?.status !== 'completed' &&
              run.presentation?.status !== 'failed'),
        )
        await refresh()
        if (cancelled) return
        attempts += 1
        if (pending.length > 0 && attempts < 375) {
          timer = window.setTimeout(poll, 800)
        } else if (attempts >= 375) {
          setError('运行结果刷新超时；服务端仍会继续，可刷新页面恢复跟踪。')
        }
      } catch (reason) {
        if (!cancelled) setError(reason instanceof Error ? reason.message : '加载运行结果失败')
      }
    }
    poll()
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [activeWorkflowRunIds, refresh])

  async function send(event: FormEvent, referenceFiles: ChatReferenceFile[] = [], workingDirectory?: string, images: ChatImageInput[] = []) {
    event.preventDefault()
    if ((!input.trim() && !images.length) || sendingRef.current) return false
    sendingRef.current = true
    const text = input.trim() || '请分析这些图片。'
    setInput('')
    setSending(true)
    setOptimisticStartedAt(Date.now())
    const optimistic: Message = {
      id: `local-${Date.now()}`,
      role: 'user',
      content: { type: 'text', text, referenceFiles, ...(images.length ? { images: images.map(image => ({ name: image.name, url: `data:${image.mimeType};base64,${image.data}` })) } : {}) },
    }
    setMessages((current) => [...current, optimistic])
    try {
      let targetId = conversationId || pendingConversationRef.current
      if (!targetId) {
        const created = await api.createConversation('新对话')
        targetId = created.id
        pendingConversationRef.current = targetId
      }
      if (!conversationId && (draftSelection.agentProfileId || draftSelection.traexModel || draftSelection.reasoningEffort || draftSelection.modelProfileId || draftSelection.model)) {
        await runtimeRequest(`/conversations/${targetId}`, 'PUT', draftSelection)
      }
      const result = await (images.length ? api.sendMessage(targetId, text, referenceFiles, workingDirectory, images) : workingDirectory ? api.sendMessage(targetId, text, referenceFiles, workingDirectory) : referenceFiles.length ? api.sendMessage(targetId, text, referenceFiles) : api.sendMessage(targetId, text))
      if (
        result.assistant.conversationRunId &&
        (result.assistant.type === 'agent_run' || result.assistant.runType === 'agent_run')
      ) {
        setActiveRunId(result.assistant.conversationRunId)
      }
      if (!conversationId) {
        pendingConversationRef.current = ''
        navigate(`/chat/${targetId}`, { replace: true })
      } else {
        await refresh()
      }
      await refreshHistory()
      return true
    } catch (reason) {
      setInput(text)
      setMessages(current => current.filter(message => message.id !== optimistic.id))
      setError(reason instanceof Error ? reason.message : '发送失败')
      return false
    } finally {
      sendingRef.current = false
      setSending(false)
      setOptimisticStartedAt(null)
    }
  }

  async function changeWorkingDirectory(value: string | undefined) {
    try {
      if (conversationId) await api.updateConversationDirectory(conversationId, value ?? null)
      setWorkingDirectory(value)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '保存工作目录失败')
    }
  }

  function createNewConversation() {
    pendingConversationRef.current = ''
    setWorkingDirectory(undefined)
    setInput('')
    setError('')
    setHistoryOpen(false)
    setDraftSelection(readSelectionPreference())
    navigate('/chat')
  }

  async function renameConversation(id: string, title: string) {
    const updated = await api.renameConversation(id, title)
    setHistory(current => current.map(item => item.id === id ? { ...item, title: updated.title } : item))
  }

  async function deleteConversation(id: string) {
    await api.deleteConversation(id)
    setHistory(current => current.filter(item => item.id !== id))
    if (id === conversationId) {
      setInput('')
      setError('')
      setHistoryOpen(false)
      const next = history.find(item => item.id !== id)
      setConversationId(next?.id ?? '')
      navigate(next ? `/chat/${next.id}` : '/chat', { replace: true })
    }
  }

  async function loadMoreHistory() {
    if (!historyCursor) return
    try {
      const page = await api.listConversations(historyCursor)
      setHistory((current) => [...current, ...page.items])
      setHistoryCursor(page.nextCursor)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '加载历史失败')
    }
  }

  async function toggleWorkflowMode() {
    if (!conversationId) return
    setSwitchingMode(true)
    setError('')
    const next = workflowMode === 'enabled' ? 'disabled' : 'enabled'
    try {
      const result = await api.updateConversationWorkflowMode(conversationId, next)
      setWorkflowMode(result.workflowMode)
      if (result.cancelledPendingSelections > 0) {
        setError('未执行的候选已关闭；切换只影响下一条消息，活动运行保持不变。')
      }
      await refreshHistory()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '切换工作流模式失败')
    } finally {
      setSwitchingMode(false)
    }
  }

  function turnFor(message: Message) {
    return turns.find((turn) => turn.id === message.turnId)
  }

  function elapsedLabel(turn?: ConversationTurn) {
    if (!turn) return null
    const elapsed =
      turn.elapsedMs ??
      Math.max(0, (turn.completedAt ? new Date(turn.completedAt).getTime() : clock) - new Date(turn.acceptedAt).getTime())
    return `${(elapsed / 1000).toFixed(1)}s`
  }

  function tokenLabel(turn?: ConversationTurn) {
    if (!turn) return null
    if (turn.usage.state === 'pending') return turn.usage.totalTokens !== null
      ? `已用 ${tokenNumber.format(turn.usage.totalTokens)} Token · 更新中`
      : 'Token 等待模型上报'
    if (turn.usage.state === 'unavailable') return 'Token 不可用'
    if (turn.usage.state === 'partial') {
      return `已统计 ${tokenNumber.format(turn.usage.totalTokens ?? 0)} Token，另有部分不可用`
    }
    return `${tokenNumber.format(turn.usage.totalTokens ?? 0)} Token`
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
      if (result.status === 'blocked' || !result.draftWorkflowId) {
        const diagnostics = result.analysis.diagnostics
          .filter((item) => item.level === 'error')
          .map((item) => `${item.code}：${item.message}`)
          .join('；')
        setError(diagnostics || '该轨迹缺少可验证事实，暂时不能发布')
        return
      }
      const draftWorkflowId = result.draftWorkflowId
      setDrafts((current) => ({
        ...current,
        [conversationRunId]: draftWorkflowId,
      }))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '固化失败')
    }
  }

  return { refresh, workingDirectory, changeWorkingDirectory, draftSelection, setDraftSelection, renameConversation, deleteConversation, turnFor, elapsedLabel, tokenLabel, conversationId, messages, history, historyCursor, historyOpen, setHistoryOpen, traceOpen, setTraceOpen, workflowMode, workflowSelectionAvailable, switchingMode, clock, optimisticStartedAt, input, setInput, sending, error, copyFeedback, drafts, candidateInputs, setCandidateInputs, confirming, startingAgent, activeRunId, runStatus, traceEvents, agentRuns, taskCheckpoints, workflowRuns, copyResult, send, createNewConversation, loadMoreHistory, toggleWorkflowMode, startComponent, startAgent, cancelActiveRun, distill }
}
