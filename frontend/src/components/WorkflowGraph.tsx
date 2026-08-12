import {
  Background,
  Controls,
  MarkerType,
  MiniMap,
  ReactFlow,
  type Edge,
  type Node,
} from '@xyflow/react'
import { useMemo } from 'react'

import type { NodeDefinition, WorkflowDefinition } from '@/api/client'
import { edgeLabel, layerNodes } from '@/lib/workflowLayout'

import '@xyflow/react/dist/style.css'

const nodeColors: Record<string, string> = {
  input: '#3f8cff',
  output: '#39c68a',
  transform: '#d6953d',
  condition: '#d9b83f',
  approval: '#dc654f',
  skillx: '#66a884',
  mcp: '#5a92b5',
  llm: '#ad72ce',
  agent: '#b47dcb',
  browser: '#da7445',
  artifact: '#82959f',
}

function toNode(item: NodeDefinition, x: number, y: number): Node {
  return {
    id: item.id,
    position: { x, y },
    data: {
      label: (
        <div className="graph-node-label">
          <small>{item.type}</small>
          <strong>{item.displayName}</strong>
        </div>
      ),
    },
    style: {
      width: 170,
      border: `1px solid ${nodeColors[item.type] ?? '#82959f'}`,
      borderLeft: `4px solid ${nodeColors[item.type] ?? '#82959f'}`,
      borderRadius: 3,
      background: '#142028',
      color: '#e8ede9',
      boxShadow: '0 8px 24px rgba(0, 0, 0, .24)',
    },
  }
}

export function WorkflowGraph({
  definition,
  compact = false,
}: {
  definition: WorkflowDefinition
  compact?: boolean
}) {
  const { nodes, edges, cyclic } = useMemo(() => {
    const layout = layerNodes(definition)
    if (layout.cyclic) return { nodes: [], edges: [], cyclic: true }
    const counts = new Map<number, number>()
    const graphNodes = definition.spec.nodes.map((item) => {
      const layer = layout.layers.get(item.id) ?? 0
      const row = counts.get(layer) ?? 0
      counts.set(layer, row + 1)
      return toNode(item, layer * 230, row * 120)
    })
    const graphEdges: Edge[] = definition.spec.edges.map((edge, index) => ({
      id: `${edge.source}-${edge.target}-${index}`,
      source: edge.source,
      target: edge.target,
      label: edgeLabel(edge.when),
      markerEnd: { type: MarkerType.ArrowClosed, color: '#72838a' },
      style: { stroke: '#72838a', strokeWidth: 1.5 },
      labelStyle: { fill: '#d8a354', fontSize: 11 },
    }))
    return { nodes: graphNodes, edges: graphEdges, cyclic: false }
  }, [definition])

  if (cyclic) {
    return (
      <div className={compact ? 'workflow-graph compact graph-error' : 'workflow-graph graph-error'}>
        CYCLIC_GRAPH：工作流包含环，已停止布局
      </div>
    )
  }

  return (
    <div className={compact ? 'workflow-graph compact' : 'workflow-graph'}>
      <ReactFlow nodes={nodes} edges={edges} fitView nodesDraggable={false}>
        <Background color="#293840" gap={24} size={1} />
        <Controls showInteractive={false} />
        {!compact && (
          <MiniMap
            nodeColor={(node) =>
              nodeColors[
                definition.spec.nodes.find((item) => item.id === node.id)?.type ?? ''
              ] ?? '#82959f'
            }
            maskColor="rgba(7, 13, 17, .72)"
          />
        )}
      </ReactFlow>
    </div>
  )
}
