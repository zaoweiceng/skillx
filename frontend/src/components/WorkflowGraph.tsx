import {
  Background,
  Handle,
  Position,
  Controls,
  MarkerType,
  MiniMap,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from '@xyflow/react'
import { useMemo } from 'react'
import { Card, Typography } from 'antd'

import type { NodeDefinition, WorkflowDefinition } from '@/api/client'
import { edgeLabel, layerNodes } from '@/lib/workflowLayout'

import '@xyflow/react/dist/style.css'

type WorkflowNodeData = { label: string; kind: string }
const nodeLabels: Record<string, string> = {
  input: '输入', output: '输出', transform: '数据处理', condition: '条件分支',
  approval: '人工确认', skillx: '能力调用', mcp: '工具调用', llm: '模型',
  agent: 'Agent', browser: '浏览器', artifact: '产物',
}

function WorkflowNode({ data }: NodeProps<Node<WorkflowNodeData>>) {
  return <>
    <Handle type="target" position={Position.Left} />
    <Card size="small" className="workflow-node-card">
      <Typography.Text type="secondary" className="workflow-node-kind">{nodeLabels[data.kind] ?? data.kind}</Typography.Text>
      <Typography.Text strong className="workflow-node-title">{data.label}</Typography.Text>
    </Card>
    <Handle type="source" position={Position.Right} />
  </>
}

const nodeTypes = { workflow: WorkflowNode }

function toNode(item: NodeDefinition, x: number, y: number): Node<WorkflowNodeData> {
  return {
    id: item.id,
    type: 'workflow',
    position: { x, y },
    data: { label: item.displayName, kind: item.type },
    style: { width: 196 },
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
    const totals = new Map<number, number>()
    layout.layers.forEach(layer => totals.set(layer, (totals.get(layer) ?? 0) + 1))
    const counts = new Map<number, number>()
    const graphNodes = definition.spec.nodes.map((item) => {
      const layer = layout.layers.get(item.id) ?? 0
      const row = counts.get(layer) ?? 0
      counts.set(layer, row + 1)
      return toNode(item, layer * 280, (row - ((totals.get(layer) ?? 1) - 1) / 2) * 140)
    })
    const graphEdges: Edge[] = definition.spec.edges.map((edge, index) => ({
      id: `${edge.source}-${edge.target}-${index}`,
      source: edge.source,
      target: edge.target,
      label: edgeLabel(edge.when),
      markerEnd: { type: MarkerType.ArrowClosed, color: 'var(--muted)' },
      style: { stroke: 'var(--muted)', strokeWidth: 1.5 },
      labelStyle: { fill: 'var(--text)', fontSize: 11 },
      labelBgStyle: { fill: 'var(--panel)' },
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
      <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} fitView fitViewOptions={{ padding: 0.25, maxZoom: 1 }} nodesDraggable={false} nodesConnectable={false}>
        <Background color="var(--line)" gap={24} size={1} />
        <Controls showInteractive={false} />
        {!compact && nodes.length > 6 && (
          <MiniMap nodeColor="var(--panel-raised)" nodeStrokeColor="var(--muted)"
            maskColor="var(--graph-mask)" />
        )}
      </ReactFlow>
    </div>
  )
}
