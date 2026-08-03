import { describe, expect, it } from 'vitest'

import type { WorkflowDefinition } from '@/api/client'
import { outputVariables, reachableUpstream } from '@/lib/variablePicker'

const definition: WorkflowDefinition = {
  apiVersion: 'workflow.platform/v1',
  kind: 'Workflow',
  metadata: {},
  spec: {
    inputSchema: {},
    outputSchema: {},
    nodes: [
      { id: 'input', type: 'input', displayName: '输入' },
      {
        id: 'format',
        type: 'transform',
        displayName: '格式化',
        config: {
          outputSchema: { type: 'object', properties: { text: { type: 'string' } } },
        },
      },
      { id: 'output', type: 'output', displayName: '输出' },
    ],
    edges: [
      { source: 'input', target: 'format' },
      { source: 'format', target: 'output' },
    ],
  },
}

describe('variablePicker', () => {
  it('只返回可达上游节点', () => {
    expect(reachableUpstream(definition, 'output')).toEqual(['input', 'format'])
    expect(reachableUpstream(definition, 'format')).toEqual(['input'])
    expect(reachableUpstream(definition, 'input')).toEqual([])
  })

  it('从 outputSchema 推断变量路径', () => {
    expect(outputVariables(definition, 'format')).toEqual(['${format.output.text}'])
    expect(outputVariables(definition, 'input')).toEqual(['${input.output}'])
  })
})
