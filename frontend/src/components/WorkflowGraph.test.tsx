import { describe, expect, it } from 'vitest'

import type { WorkflowDefinition } from '@/api/client'
import { edgeLabel, layerNodes } from '@/lib/workflowLayout'

function definition(edges: WorkflowDefinition['spec']['edges']): WorkflowDefinition {
  return {
    apiVersion: 'workflow.platform/v1',
    kind: 'Workflow',
    metadata: { name: 'layout-test' },
    spec: {
      inputSchema: { type: 'object' },
      outputSchema: { type: 'object' },
      nodes: [
        { id: 'input', type: 'input', displayName: '输入' },
        { id: 'work', type: 'transform', displayName: '处理' },
        { id: 'output', type: 'output', displayName: '输出' },
      ],
      edges,
    },
  }
}

describe('layerNodes', () => {
  it('null 与 undefined 条件边都不生成可见标签', () => {
    expect(edgeLabel(null)).toBeUndefined()
    expect(edgeLabel(undefined)).toBeUndefined()
    expect(edgeLabel(false)).toBe('false')
  })
  it('为 DAG 计算有界层级', () => {
    const result = layerNodes(
      definition([
        { source: 'input', target: 'work' },
        { source: 'work', target: 'output' },
      ]),
    )

    expect(result.cyclic).toBe(false)
    expect(result.layers.get('output')).toBe(2)
  })

  it('检测到环时立即返回诊断状态', () => {
    const result = layerNodes(
      definition([
        { source: 'input', target: 'work' },
        { source: 'work', target: 'output' },
        { source: 'output', target: 'work' },
      ]),
    )

    expect(result.cyclic).toBe(true)
  })
})
