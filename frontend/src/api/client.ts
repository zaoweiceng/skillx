export type NodeDefinition = {
  id: string
  type: string
  displayName: string
  input?: Record<string, unknown>
  config?: Record<string, unknown>
  sideEffect?: boolean
  fallback?: {
    on: string[]
    target: string
    requireUserApproval?: boolean
  } | null
}

export type WorkflowDefinition = {
  apiVersion: string
  kind: string
  metadata: Record<string, unknown>
  spec: {
    inputSchema: Record<string, unknown>
    uiSchema?: Record<string, unknown>
    outputSchema: Record<string, unknown>
    nodes: NodeDefinition[]
    edges: Array<{ source: string; target: string; when?: boolean }>
  }
}

export type ComponentSummary = {
  id: string
  name: string
  description: string
  workflowId: string
  defaultVersionId: string
  visibility: string
  executionClass: 'zero_llm' | 'hybrid' | 'agent'
  capabilityManifest?: {
    workflowRef: string
    name: string
    summary: string
    useWhen: string[]
    doNotUseWhen: string[]
    tags: string[]
    entrypoints: string[]
    manifestDigest: string
    fragments?: Array<{
      fragmentRef: string
      name: string
      summary: string
      manifestDigest: string
    }>
  } | null
  definition?: WorkflowDefinition
  businessView?: {
    sideEffectNodes: Array<{ nodeId: string; type: string; displayName?: string }>
    confirmationNodes: Array<{ nodeId: string; type: string }>
    recentSuccessRate: number | null
    recentRunCount: number
  }
  technicalView?: {
    digest: string | null
    capabilityLocks: unknown[]
    mcpLocks: unknown[]
    nodeCount: number
  }
}

export type NodeRun = {
  id: string
  nodeId: string
  type: string
  attempt: number
  status: string
  input: Record<string, unknown>
  output: Record<string, unknown> | null
  error: Record<string, unknown> | null
  startedAt?: string | null
  finishedAt?: string | null
  durationMs?: number | null
  metrics?: Record<string, unknown>
  source?: {
    stepId: string
    sourceNodeId: string
    sourceRef: string
  } | null
}

export type Approval = {
  id: string
  nodeId: string
  status: string
  snapshotDigest: string
  snapshot: {
    title: string
    description: string
    payload: Record<string, unknown>
    effect?: string
  }
}

export type Run = {
  id: string
  workflowVersionId: string | null
  planSnapshotId?: string | null
  componentId: string | null
  turnId?: string | null
  status: string
  input: Record<string, unknown>
  output: Record<string, unknown> | null
  error: Record<string, unknown> | null
  createdAt: string
  startedAt: string | null
  finishedAt: string | null
  durationMs?: number | null
  presentation?: RunPresentation | null
  presentations?: RunPresentation[]
  presentationExpected?: boolean
  modelUsage?: Array<{
    callId: string
    attempt: number
    stage: string
    provider: string | null
    model: string | null
    runtimeUsed: 'direct_llm' | 'agent_fallback' | 'agent_runtime' | 'fake_structured' | null
    runtimeDigest: string | null
    fallbackReason: string | null
    errorCode: string | null
    inputTokens: number | null
    outputTokens: number | null
    totalTokens: number | null
    complete: boolean
    attempted: boolean
    dispatched: boolean
    completed: boolean
    attemptedAt: string
    requestDispatchedAt: string | null
    completedAt: string | null
  }>
  runtimeSnapshot?: {
    runtimeSelection?: 'direct_llm' | 'agent_fallback' | 'unavailable'
    runtimeUsed?:
      | 'direct_llm'
      | 'agent_fallback'
      | 'agent_runtime'
      | 'fake_structured'
      | 'mixed'
      | null
    runtimeCalls?: string[]
    modelCalled?: boolean
    totalTokens?: number
    runtimeKind?: 'llm_runtime' | 'agent_runtime' | null
    provider?: string | null
    identity?: string | null
    runtimeDigest?: string
    policyDigest?: string
    fallbackReason?: string | null
  }
  nodes?: NodeRun[]
  approvals?: Approval[]
  artifacts?: Array<{
    id: string
    kind: string
    name: string
    mediaType: string
    size: number
    sha256: string
    downloadUrl: string
  }>
  dependencyVersions?: {
    workflowVersionId: string | null
    workflowDigest: string | null
    planSnapshotId?: string | null
    planDigest?: string | null
    planStatus?: string | null
    planExpiresAt?: string | null
    sourceMap?: Record<string, unknown>
    executionClass?: string | null
    capabilityLocks?: unknown[]
    mcpLocks?: unknown[]
    modelRuntime?: Record<string, unknown>
    lineage?: Record<string, unknown>
    revisionLineage?: Record<string, unknown>
  }
  checkpoints?: Array<{
    id: string
    status: string
    approvalStatus?: string
    failedNode: string
    fallbackTarget?: string | null
    recoveryNodeRunId?: string | null
    claimGeneration?: number
    attempt: number
    digest: string
    completedNodes: string[]
    remainingSubgraph: string[]
    committedSideEffects: Array<{ nodeId: string; idempotent: boolean }>
    decision: Record<string, unknown> | null
  }>
  failureObservations?: Array<{
    id: string
    fingerprint: string
    nodeId: string
    errorCode: string
    classification: string
    occurrence: number
    recovered: boolean
    baselineReproduced: boolean
    checkpointId?: string | null
    replayEvidence: Array<{
      id: string
      passed: boolean
      fixtureDigest?: string | null
      baseVersionId?: string | null
      patchOfObservationId?: string | null
      baseDefinitionDigest?: string | null
      patchDigest?: string | null
      patchedDefinitionDigest?: string | null
      proposalId?: string | null
      humanPatchId?: string | null
    }>
  }>
}

export type RunPresentation = {
  id: string
  revision: number
  status: 'pending' | 'running' | 'completed' | 'failed'
  text: string | null
  producer: 'template' | 'llm'
  model: string | null
  error: Record<string, unknown> | null
  startedAt: string | null
  completedAt: string | null
  elapsedMs: number | null
}

export type TokenSummary = {
  state: 'pending' | 'zero' | 'complete' | 'partial' | 'unavailable'
  totalTokens: number | null
  stages: Array<{
    stage: string
    provider: string | null
    model: string | null
    totalTokens: number | null
    complete: boolean
  }>
}

export type ConversationTurn = {
  id: string
  workflowModeSnapshot: 'enabled' | 'disabled'
  status: string
  acceptedAt: string
  firstResponseAt: string | null
  completedAt: string | null
  elapsedMs: number | null
  usage: TokenSummary
}

export type ConversationSummary = {
  id: string
  title: string
  preview: string
  lastMessageAt: string | null
  status: string
  workflowMode: 'enabled' | 'disabled'
}

export type Workflow = {
  id: string
  name: string
  draftRevision: number
  draftDigest: string
  draftMetadata?: {
    status?: string
    replayEvidenceId?: string
  }
  definition: WorkflowDefinition
  createdAt: string
  updatedAt: string
}

export type WorkflowVersionSummary = {
  id: string
  workflowId: string
  version: number
  digest: string
  definition: WorkflowDefinition
  presentationPolicy: Record<string, unknown>
  createdAt: string
}

export type ChatMessageContent = {
  type: string
  state?: string
  text: string
  code?: string
  componentId?: string
  componentName?: string
  workflowVersionId?: string
  runId?: string
  conversationRunId?: string
  runType?: string
  sourceRunId?: string
  rootSourceRunId?: string
  continuationOfConversationRunId?: string
  taskCheckpointId?: string
  continuationGeneration?: number
  required?: string[]
  confidence?: number
  reason?: string
  candidateSnapshotDigest?: string
  offerAgentRun?: boolean
  inputSchema?: Record<string, unknown>
  uiSchema?: Record<string, unknown>
  executionClass?: string
  level?: string
  items?: WorkflowCapabilityCard[]
  agentAction?: string
  selectionReason?: string
  disclosureSessionId?: string
  disclosureLevel?: string
  pendingSelectionId?: string
  resumedPendingSelection?: boolean
  workflowModeSnapshot?: 'enabled' | 'disabled'
  selectionMode?: 'real' | 'fixture' | 'unavailable'
  bindingSources?: Array<{
    field: string
    source: string
    messageId?: string
    quote: string
    confidence: number
    uncertain: boolean
    serverVerified?: boolean
    normalizationType?: string | null
  }>
  refs?: string[]
  toolTimeline?: Array<{
    type: string
    callId?: string
    capabilityRef?: string
    level?: string
    status?: string
    diagnostics?: Array<{ level: string; code: string; message: string }>
  }>
}

export type WorkflowCapabilityCard = {
  workflowRef: string
  name: string
  summary: string
  tags: string[]
  inputSynopsis: {
    required: string[]
    properties: Record<string, string>
  }
  outputSynopsis: {
    properties: Record<string, string>
  }
  executionClass: string
  sideEffects: string
  approvalRequired: boolean
  estimatedCost: { llmCalls: number }
  manifestDigest: string
  matchReason: { score: number; query: string; summary?: string }
}

export type ConversationRunEvent = {
  seq: number
  type: string
  payload: Record<string, unknown>
  createdAt?: string
}

export type RunEvent = ConversationRunEvent & {
  id?: string
  runId?: string
}

export type ConversationRunSummary = {
  id: string
  conversationId: string
  status: string
  runType: string
  input: Record<string, unknown>
  output: Record<string, unknown>
  error: Record<string, unknown> | null
}

export type TaskCheckpointSummary = {
  id: string
  sourceId: string
  status: string
  continuationRunId: string | null
  continuationStatus: string | null
  continuationOutputDigest: string | null
  continuationGeneration: number
  sourceCheckpointId: string | null
  childCheckpointId: string | null
  nextContinuationRunId: string | null
  chainStatus: string
  chainError: { code?: string; message?: string } | null
  pendingQuestion: string | null
  lineage: Array<{
    checkpointId: string
    sourceRunId: string
    continuationRunId: string | null
    generation: number
    status: string
    continuationStatus: string | null
    childCheckpointId: string | null
  }>
}

const API_BASE = import.meta.env.VITE_API_BASE ?? '/api'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      'content-type': 'application/json',
      ...init?.headers,
    },
  })
  if (!response.ok) {
    const payload = await response.json().catch(() => ({ detail: response.statusText }))
    const detail = payload.error?.message ?? payload.detail?.message ?? payload.detail
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
  }
  return response.json() as Promise<T>
}

export const api = {
  getHealth: () =>
    request<{
      status: string
      migration: string
      workflowSelectionMode: 'real' | 'fixture' | 'unavailable'
      selectionProvider: {
        type: 'real' | 'fixture' | 'unavailable'
        runtimeType?: 'traex_managed' | 'openai_sidecar' | 'fixture' | 'unavailable'
        protocolTestOnly?: boolean
        configured: boolean
        protocolAvailable: boolean
        modelAvailable: boolean
        model?: string | null
        digest?: string
        adapterDigest?: string
        adapterVersion?: string
        adapterTrusted?: boolean
        source?: string
        lastProbe: {
          at: string | null
          ok: boolean
          summary: string
          kind?: string
          probeId?: string
          model?: string
          totalTokens?: number
          usageComplete?: boolean
          requestDispatched?: boolean
          code?: string
        }
        nextProbeAllowedAt: string | null
      }
      agentRuntime?: {
        type: 'real' | 'fixture' | 'unavailable'
        runtimeType: 'traex_managed' | 'openai_sidecar' | 'fixture' | 'unavailable'
        protocolTestOnly?: boolean
        configured: boolean
        protocolAvailable: boolean
        modelAvailable: boolean
        model: string | null
        digest: string
        adapterDigest?: string
        adapterVersion?: string
        adapterTrusted?: boolean
        source: string
        lastProbe: {
          at: string | null
          ok: boolean
          summary: string
          totalTokens?: number
        }
        nextProbeAllowedAt: string | null
      }
      llmRuntime?: {
        type: 'openai_compatible' | 'unavailable'
        configured: boolean
        protocol: string
        model: string | null
        digest: string
        source: string
        probe: { kind: string; ok: boolean; summary: string }
      }
      modelRuntimeFallbackPolicy?: {
        order: string[]
        trigger: string
        executionErrorsFallback: boolean
      }
      runtimes: Record<
        string,
        {
          status: string
          detail: string
          workerId?: string | null
          checkedAt?: string | null
          stale?: boolean
          chromiumExecutable?: string | null
          chromiumVersion?: string | null
          playwrightVersion?: string | null
          launchSucceeded?: boolean
        }
      >
    }>('/health/ready'),
  probeProvider: () =>
    request<{
      at: string
      ok: boolean
      summary: string
      probeId: string
      model: string
      totalTokens?: number
      usageComplete?: boolean
      code?: string
      nextProbeAllowedAt: string
    }>('/health/provider-probe', { method: 'POST' }),
  listComponents: () => request<ComponentSummary[]>('/components'),
  getComponent: (id: string) => request<ComponentSummary>(`/components/${id}`),
  createRun: (id: string, input: Record<string, unknown>) =>
    request<Run>(`/components/${id}/runs`, {
      method: 'POST',
      body: JSON.stringify({ input }),
    }),
  listRuns: (filters: {
    componentId?: string
    workflowVersionId?: string
    status?: string
    cursor?: string
    limit?: number
  } = {}) => {
    const params = new URLSearchParams()
    Object.entries(filters).forEach(([key, value]) => {
      if (value !== undefined) params.set(key, String(value))
    })
    return request<{ items: Run[]; nextCursor: string | null }>(
      `/runs${params.size ? `?${params}` : ''}`,
    )
  },
  getRun: (id: string) => request<Run>(`/runs/${id}`),
  listRunEvents: (id: string, after = 0, limit = 200) =>
    request<RunEvent[]>(
      `/runs/${id}/events?after=${after}&limit=${limit}`,
    ),
  getRunTechnicalError: (id: string) =>
    request<{
      runId: string
      errors: Array<{
        scope: 'run' | 'node'
        ref: string
        nodeId?: string
        attempt?: number
        code: string
        userMessage: string
        action: string
        evidenceDigest: string
      }>
      evidence: Array<{
        digest: string
        detail: Record<string, unknown>
        refs: string[]
      }>
      truncated?: boolean
    }>(`/runs/${id}/technical-error`),
  createWorkflowSession: (conversationId?: string) =>
    request<{ id: string; status: string; expiresAt: string }>('/workflow-tools/sessions', {
      method: 'POST',
      body: JSON.stringify({ conversationId }),
    }),
  searchWorkflows: (
    sessionId: string,
    query: string,
    constraints: Record<string, unknown> = {},
  ) =>
    request<{ level: string; count: number; items: WorkflowCapabilityCard[] }>(
      '/workflow-tools/search',
      {
        method: 'POST',
        body: JSON.stringify({ sessionId, query, constraints, limit: 8 }),
      },
    ),
  describeWorkflow: (sessionId: string, ref: string) =>
    request<Record<string, unknown>>('/workflow-tools/describe', {
      method: 'POST',
      body: JSON.stringify({ sessionId, ref }),
    }),
  describeFragment: (sessionId: string, ref: string) =>
    request<Record<string, unknown>>('/workflow-tools/describe-fragment', {
      method: 'POST',
      body: JSON.stringify({ sessionId, ref }),
    }),
  cancelRun: (id: string) => request<Run>(`/runs/${id}/cancel`, { method: 'POST' }),
  approve: (id: string, snapshotDigest: string) =>
    request<{ id: string; status: string }>(`/approvals/${id}/approve`, {
      method: 'POST',
      body: JSON.stringify({ snapshotDigest }),
    }),
  reject: (id: string) =>
    request<{ id: string; status: string }>(`/approvals/${id}/reject`, {
      method: 'POST',
    }),
  listWorkflows: () => request<Workflow[]>('/workflows'),
  getWorkflow: (id: string) => request<Workflow>(`/workflows/${id}`),
  listWorkflowVersions: (id: string) =>
    request<WorkflowVersionSummary[]>(`/workflows/${id}/versions`),
  createWorkflowVersionRun: (id: string, input: Record<string, unknown>) =>
    request<Run>(`/workflow-versions/${id}/runs`, {
      method: 'POST',
      body: JSON.stringify({ input }),
    }),
  createWorkflow: (name: string, definition: WorkflowDefinition) =>
    request<Workflow>('/workflows', {
      method: 'POST',
      body: JSON.stringify({ name, definition }),
    }),
  updateDraft: (id: string, revision: number, definition: WorkflowDefinition) =>
    request<Workflow>(`/workflows/${id}/draft`, {
      method: 'PUT',
      body: JSON.stringify({ revision, definition }),
    }),
  validateWorkflow: (id: string) =>
    request<{
      valid: boolean
      digest?: string
      diagnostics: Array<{ level: string; code: string; path: string; message: string }>
    }>(`/workflows/${id}/validate`, { method: 'POST' }),
  publishWorkflow: (
    id: string,
    draftRevision: number,
    draftDigest: string,
    replayEvidenceId?: string,
  ) =>
    request<{ id: string; version: number; digest: string }>(
      `/workflows/${id}/publish`,
      {
        method: 'POST',
        body: JSON.stringify({ draftRevision, draftDigest, replayEvidenceId }),
      },
    ),
  applyContractPatch: (
    id: string,
    nodeId: string,
    patch: Record<string, unknown>,
  ) =>
    request<{ id: string; draftRevision: number; definition: WorkflowDefinition }>(
      `/workflows/${id}/contract-patch`,
      { method: 'POST', body: JSON.stringify({ nodeId, patch }) },
    ),
  applyWorkflowPatch: (
    versionId: string,
    body: {
      sourceRunId: string
      proposalId: string
      proposalDigest: string
      patch?: {
        nodePatches?: Array<{ nodeId: string; changes: Record<string, unknown> }>
        addEdges?: Array<{ source: string; target: string }>
        removeEdges?: Array<{ source: string; target: string }>
        reason?: string
      }
    },
  ) =>
    request<{
      draftWorkflowId: string
      baseVersionId: string
      definition: WorkflowDefinition
      diagnostics: Array<{ level: string; code: string; path: string; message: string }>
      eligibility?: { eligible: boolean; reason: string; occurrence: number }
      replay?: { replayRunId: string; passed: boolean; failedNodeStatus: string }
      publishable: boolean
    }>(`/workflow-versions/${versionId}/workflow-patch`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  proposeRevision: (observationId: string) =>
    request<{
      proposalId: string
      proposalDigest: string
      proposal: {
        nodePatches?: Array<{ nodeId: string; changes: Record<string, unknown> }>
        addEdges?: Array<{ source: string; target: string }>
        removeEdges?: Array<{ source: string; target: string }>
        reason?: string
      }
    }>(`/failure-observations/${observationId}/revision-proposal`, {
      method: 'POST',
    }),
  createComponent: (
    name: string,
    description: string,
    workflowVersionId: string,
  ) =>
    request<ComponentSummary>('/components', {
      method: 'POST',
      body: JSON.stringify({
        name,
        description,
        workflowVersionId,
        visibility: 'private',
      }),
    }),
  listCapabilities: () =>
    request<
      Array<{
        id: string
        name: string
        type: string
        visibility: string
        currentVersionId: string | null
      }>
    >('/capabilities'),
  createConversation: (title: string) =>
    request<{ id: string; title: string; workflowMode: 'enabled' | 'disabled' }>(
      '/conversations',
      {
      method: 'POST',
      body: JSON.stringify({ title }),
      },
    ),
  listConversations: (cursor?: string, query?: string) => {
    const params = new URLSearchParams({ limit: '30' })
    if (cursor) params.set('cursor', cursor)
    if (query) params.set('query', query)
    return request<{ items: ConversationSummary[]; nextCursor: string | null }>(
      `/conversations?${params}`,
    )
  },
  updateConversationWorkflowMode: (
    id: string,
    workflowMode: 'enabled' | 'disabled',
  ) =>
    request<{
      id: string
      workflowMode: 'enabled' | 'disabled'
      appliesFrom: string
      cancelledPendingSelections: number
    }>(`/conversations/${id}/workflow-mode`, {
      method: 'PATCH',
      body: JSON.stringify({ workflowMode }),
    }),
  getConversation: (id: string) =>
    request<{
      id: string
      title: string
      boundComponentId: string | null
      workflowMode: 'enabled' | 'disabled'
      lastMessageAt: string | null
      messages: Array<{
        id: string
        role: string
        content: ChatMessageContent
        turnId: string | null
        createdAt: string
      }>
      turns: ConversationTurn[]
    }>(`/conversations/${id}`),
  sendMessage: (id: string, content: string) =>
    request<{
      userMessageId: string
      assistant: ChatMessageContent
      turn: ConversationTurn
    }>(
      `/conversations/${id}/messages`,
      { method: 'POST', body: JSON.stringify({ content }) },
    ),
  confirmComponent: (
    conversationId: string,
    payload: {
      componentId: string
      candidateSnapshotDigest: string
      inputs: Record<string, unknown>
    },
  ) =>
    request<{ runId: string; assistant: ChatMessageContent }>(
      `/conversations/${conversationId}/confirm-component`,
      { method: 'POST', body: JSON.stringify(payload) },
    ),
  startAgentRun: (
    conversationId: string,
    content: string,
  ) =>
    request<{ conversationRunId: string; status: string; assistant: ChatMessageContent }>(
      `/conversations/${conversationId}/agent-runs`,
      { method: 'POST', body: JSON.stringify({ content }) },
    ),
  getConversationRun: (id: string) =>
    request<ConversationRunSummary>(`/conversation-runs/${id}`),
  getTaskCheckpoint: (id: string) =>
    request<TaskCheckpointSummary>(`/task-checkpoints/${id}`),
  cancelConversationRun: (id: string) =>
    request<{ conversationRunId: string; status: string }>(
      `/conversation-runs/${id}/cancel`,
      { method: 'POST' },
    ),
  listConversationRunEvents: (id: string, after = 0) =>
    request<ConversationRunEvent[]>(
      `/conversation-runs/${id}/events?after=${after}`,
    ),
  distillConversationRun: (id: string) =>
    request<{
      id: string
      status: string
      draftWorkflowId: string
      analysis: {
        directNodes: number
        llmOrAgentNodes: number
        estimatedLlmCallsBefore: number
        estimatedLlmCallsAfter: number
        publishable: boolean
        diagnostics: Array<{ level: string; code: string; message: string }>
      }
    }>(`/conversation-runs/${id}/distill`, { method: 'POST' }),
}

export function eventStreamUrl(runId: string, after = 0): string {
  return `${API_BASE}/runs/${runId}/events/stream?after=${after}`
}

export function conversationRunUrl(runId: string): string {
  return `${API_BASE}/conversation-runs/${runId}`
}

export function taskCheckpointUrl(checkpointId: string): string {
  return `${API_BASE}/task-checkpoints/${checkpointId}`
}
