import {
  addEdge,
  applyNodeChanges,
  Background,
  Controls,
  MarkerType,
  MiniMap,
  ReactFlow,
  type Connection,
  type Edge,
  type Node,
  type NodeChange,
} from '@xyflow/react'
import { useCallback, useMemo } from 'react'

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

function toNode(item: NodeDefinition, x: number, y: number, selected: boolean): Node {
  return {
    id: item.id,
    position: { x, y },
    selected,
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
      border: `1px solid ${selected ? 'var(--control-border)' : 'var(--line)'}`,
      borderLeft: `4px solid ${nodeColors[item.type] ?? '#82959f'}`,
      borderRadius: 10,
      background: selected ? 'var(--control-selected)' : 'var(--panel)',
      color: 'var(--text)',
      boxShadow: selected ? '0 0 0 2px var(--focus)' : '0 2px 8px rgba(0, 0, 0, .06)',
    },
  }
}

/**
 * 可交互编辑画布：支持鼠标连线、选中、删除节点/连线，并把结果回调给上层结构化 state。
 * 连线合法性（环检测/可达性）由上层 connectNodes 判定，非法连线不写入。
 */
export function EditableCanvas({
  definition,
  selectedNodeId,
  onSelectNode,
  onConnect,
  onDeleteNode,
  onDeleteEdge,
}: {
  definition: WorkflowDefinition
  selectedNodeId: string
  onSelectNode: (nodeId: string) => void
  onConnect: (source: string, target: string) => void
  onDeleteNode: (nodeId: string) => void
  onDeleteEdge: (source: string, target: string) => void
}) {
  const { nodes, edges, cyclic } = useMemo(() => {
    const layout = layerNodes(definition)
    if (layout.cyclic) return { nodes: [] as Node[], edges: [] as Edge[], cyclic: true }
    const counts = new Map<number, number>()
    const graphNodes = definition.spec.nodes.map((item) => {
      const layer = layout.layers.get(item.id) ?? 0
      const row = counts.get(layer) ?? 0
      counts.set(layer, row + 1)
      return toNode(item, layer * 230, row * 120, item.id === selectedNodeId)
    })
    const graphEdges: Edge[] = definition.spec.edges.map((edge, index) => ({
      id: `${edge.source}-${edge.target}-${index}`,
      source: edge.source,
      target: edge.target,
      label: edgeLabel(edge.when),
      markerEnd: { type: MarkerType.ArrowClosed, color: '#72838a' },
      style: { stroke: '#72838a', strokeWidth: 1.5 },
    }))
    return { nodes: graphNodes, edges: graphEdges, cyclic: false }
  }, [definition, selectedNodeId])

  const handleConnect = useCallback(
    (connection: Connection) => {
      if (connection.source && connection.target) {
        onConnect(connection.source, connection.target)
      }
      // 不直接改内部状态：由上层 connectNodes 校验后回写结构化 state。
      return addEdge(connection, edges)
    },
    [edges, onConnect],
  )

  const handleNodesChange = useCallback(
    (changes: NodeChange[]) => {
      for (const change of changes) {
        if (change.type === 'remove') onDeleteNode(change.id)
        if (change.type === 'select' && change.selected) onSelectNode(change.id)
      }
      applyNodeChanges(changes, nodes)
    },
    [nodes, onDeleteNode, onSelectNode],
  )

  const handleEdgesDelete = useCallback(
    (deleted: Edge[]) => {
      for (const edge of deleted) onDeleteEdge(edge.source, edge.target)
    },
    [onDeleteEdge],
  )

  if (cyclic) {
    return (
      <div className="workflow-graph graph-error">CYCLIC_GRAPH：工作流包含环，已停止布局</div>
    )
  }

  return (
    <div className="workflow-graph editable">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        fitView
        onConnect={handleConnect}
        onNodesChange={handleNodesChange}
        onEdgesDelete={handleEdgesDelete}
        onNodeClick={(_, node) => onSelectNode(node.id)}
        deleteKeyCode={['Backspace', 'Delete']}
      >
        <Background color="var(--line-soft)" gap={24} size={1} />
        <Controls showInteractive={false} />
        <MiniMap
          nodeColor={(node) =>
            nodeColors[definition.spec.nodes.find((item) => item.id === node.id)?.type ?? ''] ??
            '#82959f'
          }
          maskColor="var(--graph-mask)"
        />
      </ReactFlow>
    </div>
  )
}
