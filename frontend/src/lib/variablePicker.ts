import type { WorkflowDefinition } from '@/api/client'

/**
 * 返回目标节点所有可达上游节点的 id（含传递依赖），用于 VariablePicker 只展示
 * 真正可引用的上游输出，避免引用尚未执行或不可达的节点。
 */
export function reachableUpstream(
  definition: WorkflowDefinition,
  targetId: string,
): string[] {
  const parents = new Map<string, string[]>()
  definition.spec.nodes.forEach((node) => parents.set(node.id, []))
  definition.spec.edges.forEach((edge) => {
    parents.get(edge.target)?.push(edge.source)
  })
  const seen = new Set<string>()
  const stack = [...(parents.get(targetId) ?? [])]
  while (stack.length) {
    const current = stack.pop()
    if (current === undefined || seen.has(current)) continue
    seen.add(current)
    stack.push(...(parents.get(current) ?? []))
  }
  // 保持定义顺序，便于 UI 稳定展示。
  return definition.spec.nodes
    .filter((node) => seen.has(node.id))
    .map((node) => node.id)
}

/**
 * 从节点输出 Schema 推断可引用变量路径。若无 outputSchema，则回退到通用 output 引用。
 */
export function outputVariables(
  definition: WorkflowDefinition,
  nodeId: string,
): string[] {
  const node = definition.spec.nodes.find((item) => item.id === nodeId)
  if (!node) return []
  const schema = (node.config?.outputSchema ?? node.config?.output_schema) as
    | { properties?: Record<string, unknown> }
    | undefined
  if (schema?.properties) {
    return Object.keys(schema.properties).map((key) => `\${${nodeId}.output.${key}}`)
  }
  return [`\${${nodeId}.output}`]
}
