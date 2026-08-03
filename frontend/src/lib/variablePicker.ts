import type { WorkflowDefinition } from '@/api/client'

type JsonSchema = {
  type?: string
  properties?: Record<string, JsonSchema>
  items?: JsonSchema
}

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
 * 从 JSON Schema 展开可引用的路径后缀，支持嵌套对象与数组。
 * 例如 { user: { name: string }, tags: [string] } →
 *   ['.user', '.user.name', '.tags', '.tags[]']
 */
function expandSchemaPaths(schema: JsonSchema | undefined, prefix: string, depth = 0): string[] {
  if (!schema || depth > 4) return []
  const paths: string[] = []
  if (schema.type === 'object' && schema.properties) {
    for (const [key, child] of Object.entries(schema.properties)) {
      const path = `${prefix}.${key}`
      paths.push(path)
      paths.push(...expandSchemaPaths(child, path, depth + 1))
    }
  } else if (schema.type === 'array' && schema.items) {
    const path = `${prefix}[]`
    paths.push(path)
    paths.push(...expandSchemaPaths(schema.items, path, depth + 1))
  }
  return paths
}

/**
 * 推断某节点可被下游引用的变量路径。
 *
 * - Input 节点：变量来自 workflow `inputSchema`，生成 `${input.<字段>}`，绝不生成
 *   `${input.output}`（运行时无该路径）。
 * - 其他节点：变量来自节点锁定的 `outputSchema`，生成 `${nodeId.output.<字段>}`；
 *   支持嵌套对象与数组路径。无 outputSchema 时回退到 `${nodeId.output}`。
 */
export function outputVariables(
  definition: WorkflowDefinition,
  nodeId: string,
): string[] {
  const node = definition.spec.nodes.find((item) => item.id === nodeId)
  if (!node) return []
  if (node.type === 'input') {
    const inputSchema = definition.spec.inputSchema as JsonSchema | undefined
    const paths = expandSchemaPaths(inputSchema, '')
    if (paths.length === 0) return []
    return paths.map((path) => `\${input${path}}`)
  }
  const schema = (node.config?.outputSchema ?? node.config?.output_schema) as JsonSchema | undefined
  const paths = expandSchemaPaths(schema, '')
  if (paths.length === 0) {
    return [`\${${nodeId}.output}`]
  }
  return paths.map((path) => `\${${nodeId}.output${path}}`)
}

/**
 * 判断把某上游变量映射到目标字段时的类型兼容性，供 VariablePicker 给出提示。
 * 返回 'compatible' | 'incompatible' | 'unknown'。仅比较可确定的标量/容器类型。
 */
export function typeCompatibility(
  sourceType: string | undefined,
  targetType: string | undefined,
): 'compatible' | 'incompatible' | 'unknown' {
  if (!sourceType || !targetType) return 'unknown'
  if (sourceType === targetType) return 'compatible'
  // number 可赋给 integer 目标以外，其余不同标量类型视为不兼容。
  if (sourceType === 'integer' && targetType === 'number') return 'compatible'
  return 'incompatible'
}
