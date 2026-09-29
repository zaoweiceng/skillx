import type { NodeDefinition, WorkflowDefinition } from '@/api/client'
import { layerNodes } from '@/lib/workflowLayout'

// 004-E：以 WorkflowDefinition 结构化 state 为唯一真相的编辑操作。
// 这些函数是纯函数（输入定义 → 新定义或错误），便于单测与前端复用；JSON 仅作为
// 高级导入/导出视图，不再是编辑的主状态。

export type EditResult =
  | { ok: true; definition: WorkflowDefinition }
  | { ok: false; error: string }

// MVP 可拖入的节点类型及默认配置。input/output 由模板固定，不在调色板中重复创建。
export const PALETTE_NODE_TYPES = [
  'transform',
  'condition',
  'mcp',
  'skillx',
  'workflow_call',
  'capability_call',
  'llm',
  'agent',
  'approval',
  'browser',
  'artifact',
] as const

export type PaletteNodeType = (typeof PALETTE_NODE_TYPES)[number]

const TYPE_LABELS: Record<string, string> = {
  transform: '转换',
  condition: '条件',
  mcp: 'MCP 工具',
  skillx: 'SkillX 能力',
  workflow_call: '子工作流',
  capability_call: '调用能力',
  llm: 'LLM',
  agent: '受限 Agent',
  approval: '人工确认',
  browser: '浏览器',
  artifact: '产物',
}

function clone(definition: WorkflowDefinition): WorkflowDefinition {
  return JSON.parse(JSON.stringify(definition)) as WorkflowDefinition
}

function uniqueId(base: string, taken: Set<string>): string {
  const normalized = base.replace(/[^a-zA-Z0-9_-]/g, '-').replace(/^-+|-+$/g, '') || 'node'
  let candidate = normalized
  let index = 1
  while (taken.has(candidate)) {
    index += 1
    candidate = `${normalized}-${index}`
  }
  return candidate
}

/** 新增一个调色板节点，返回新定义与新节点 id。 */
export function addNode(
  definition: WorkflowDefinition,
  type: PaletteNodeType,
): { definition: WorkflowDefinition; nodeId: string } {
  const next = clone(definition)
  const taken = new Set(next.spec.nodes.map((node) => node.id))
  const nodeId = uniqueId(type, taken)
  next.spec.nodes.push({
    id: nodeId,
    type,
    displayName: TYPE_LABELS[type] ?? type,
    input: {},
    config: {},
  })
  return { definition: next, nodeId }
}

/** 删除节点及其相关连线；input/output 节点不可删除。 */
export function deleteNode(definition: WorkflowDefinition, nodeId: string): EditResult {
  const node = definition.spec.nodes.find((item) => item.id === nodeId)
  if (!node) return { ok: false, error: '节点不存在' }
  if (node.type === 'input' || node.type === 'output') {
    return { ok: false, error: 'input/output 节点不可删除' }
  }
  const next = clone(definition)
  next.spec.nodes = next.spec.nodes.filter((item) => item.id !== nodeId)
  next.spec.edges = next.spec.edges.filter(
    (edge) => edge.source !== nodeId && edge.target !== nodeId,
  )
  return { ok: true, definition: next }
}

/**
 * 新增一条连线，立即做环检测与基础可达性校验；失败不写入 state。
 * - 不允许自环、重复边、指向 input、从 output 出发；
 * - 新边加入后若产生环则拒绝。
 */
export function connectNodes(
  definition: WorkflowDefinition,
  source: string,
  target: string,
): EditResult {
  if (source === target) return { ok: false, error: '不允许自环' }
  const ids = new Set(definition.spec.nodes.map((node) => node.id))
  if (!ids.has(source) || !ids.has(target)) {
    return { ok: false, error: '连线端点节点不存在' }
  }
  const targetNode = definition.spec.nodes.find((node) => node.id === target)
  const sourceNode = definition.spec.nodes.find((node) => node.id === source)
  if (targetNode?.type === 'input') return { ok: false, error: 'input 节点不能作为目标' }
  if (sourceNode?.type === 'output') return { ok: false, error: 'output 节点不能作为来源' }
  const duplicate = definition.spec.edges.some(
    (edge) => edge.source === source && edge.target === target,
  )
  if (duplicate) return { ok: false, error: '连线已存在' }
  const next = clone(definition)
  next.spec.edges.push({ source, target })
  if (layerNodes(next).cyclic) {
    return { ok: false, error: '该连线会形成环，已拒绝' }
  }
  return { ok: true, definition: next }
}

/** 删除一条连线。 */
export function disconnectNodes(
  definition: WorkflowDefinition,
  source: string,
  target: string,
): WorkflowDefinition {
  const next = clone(definition)
  next.spec.edges = next.spec.edges.filter(
    (edge) => !(edge.source === source && edge.target === target),
  )
  return next
}

/** 就地更新某节点的字段（displayName/config/input/sideEffect 等）。 */
export function updateNode(
  definition: WorkflowDefinition,
  nodeId: string,
  patch: Partial<NodeDefinition>,
): WorkflowDefinition {
  const next = clone(definition)
  next.spec.nodes = next.spec.nodes.map((node) =>
    node.id === nodeId ? { ...node, ...patch } : node,
  )
  return next
}

/** 设置某节点某个输入映射字段（VariablePicker 插入具体 mapping）。 */
export function setInputMapping(
  definition: WorkflowDefinition,
  nodeId: string,
  field: string,
  expression: string,
): WorkflowDefinition {
  const next = clone(definition)
  next.spec.nodes = next.spec.nodes.map((node) =>
    node.id === nodeId
      ? { ...node, input: { ...(node.input ?? {}), [field]: expression } }
      : node,
  )
  return next
}
