export type NodeDefinition = {
  id: string
  type: string
  displayName: string
  input?: Record<string, unknown>
  config?: Record<string, unknown>
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
  definition?: WorkflowDefinition
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
  workflowVersionId: string
  componentId: string | null
  status: string
  input: Record<string, unknown>
  output: Record<string, unknown> | null
  error: Record<string, unknown> | null
  createdAt: string
  startedAt: string | null
  finishedAt: string | null
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
}

export type Workflow = {
  id: string
  name: string
  draftRevision: number
  definition: WorkflowDefinition
  createdAt: string
  updatedAt: string
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
  listComponents: () => request<ComponentSummary[]>('/components'),
  getComponent: (id: string) => request<ComponentSummary>(`/components/${id}`),
  createRun: (id: string, input: Record<string, unknown>) =>
    request<Run>(`/components/${id}/runs`, {
      method: 'POST',
      body: JSON.stringify({ input }),
    }),
  listRuns: () => request<Run[]>('/runs'),
  getRun: (id: string) => request<Run>(`/runs/${id}`),
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
  publishWorkflow: (id: string) =>
    request<{ id: string; version: number; digest: string }>(
      `/workflows/${id}/publish`,
      { method: 'POST' },
    ),
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
    request<{ id: string; title: string }>('/conversations', {
      method: 'POST',
      body: JSON.stringify({ title }),
    }),
  getConversation: (id: string) =>
    request<{
      id: string
      title: string
      boundComponentId: string | null
      messages: Array<{
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
      }>
    }>(`/conversations/${id}`),
  sendMessage: (id: string, content: string) =>
    request<{ userMessageId: string; assistant: { type: string; text: string } }>(
      `/conversations/${id}/messages`,
      { method: 'POST', body: JSON.stringify({ content }) },
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

export function eventStreamUrl(runId: string): string {
  return `${API_BASE}/runs/${runId}/events/stream`
}
