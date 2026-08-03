import type { WorkflowDefinition } from '@/api/client'

export function layerNodes(definition: WorkflowDefinition): {
  layers: Map<string, number>
  cyclic: boolean
} {
  const layers = new Map<string, number>()
  const indegree = new Map<string, number>()
  const outgoing = new Map<string, string[]>()
  definition.spec.nodes.forEach((node) => {
    indegree.set(node.id, 0)
    outgoing.set(node.id, [])
  })
  definition.spec.edges.forEach((edge) => {
    if (!indegree.has(edge.source) || !indegree.has(edge.target)) return
    indegree.set(edge.target, (indegree.get(edge.target) ?? 0) + 1)
    outgoing.get(edge.source)?.push(edge.target)
  })
  const queue = definition.spec.nodes
    .filter((node) => indegree.get(node.id) === 0)
    .map((node) => node.id)
  queue.forEach((nodeId) => layers.set(nodeId, 0))
  let processed = 0
  while (queue.length) {
    const nodeId = queue.shift()
    if (!nodeId) break
    processed += 1
    for (const target of outgoing.get(nodeId) ?? []) {
      layers.set(
        target,
        Math.max(layers.get(target) ?? 0, (layers.get(nodeId) ?? 0) + 1),
      )
      const nextDegree = (indegree.get(target) ?? 0) - 1
      indegree.set(target, nextDegree)
      if (nextDegree === 0) queue.push(target)
    }
  }
  return {
    layers,
    cyclic: processed !== definition.spec.nodes.length,
  }
}
