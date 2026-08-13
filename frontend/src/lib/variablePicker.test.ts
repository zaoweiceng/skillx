import { describe, expect, it } from 'vitest'

import type { WorkflowDefinition } from '@/api/client'
import { outputVariables, reachableUpstream, typeCompatibility } from '@/lib/variablePicker'

const definition: WorkflowDefinition = {
  apiVersion: 'workflow.platform/v1',
  kind: 'Workflow',
  metadata: {},
  spec: {
    inputSchema: {
      type: 'object',
      properties: {
        name: { type: 'string' },
        profile: {
          type: 'object',
          properties: { age: { type: 'number' } },
        },
      },
    },
    outputSchema: {},
    nodes: [
      { id: 'input', type: 'input', displayName: '输入' },
      {
        id: 'format',
        type: 'transform',
        displayName: '格式化',
        config: {
          outputSchema: {
            type: 'object',
            properties: {
              text: { type: 'string' },
              tags: { type: 'array', items: { type: 'string' } },
            },
          },
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

  it('Input 节点使用 workflow inputSchema，生成 ${input.field}', () => {
    expect(outputVariables(definition, 'input')).toEqual([
      '${input.name}',
      '${input.profile}',
      '${input.profile.age}',
    ])
  })

  it('其他节点从 outputSchema 推断变量路径，数组提供整体与索引引用', () => {
    expect(outputVariables(definition, 'format')).toEqual([
      '${format.output}',
      '${format.output.text}',
      '${format.output.tags}',
      '${format.output.tags[0]}',
    ])
  })

  it('无 outputSchema 回退到 ${nodeId.output}', () => {
    const bare: WorkflowDefinition = {
      ...definition,
      spec: {
        ...definition.spec,
        nodes: [{ id: 'noschema', type: 'transform', displayName: '无契约' }],
        edges: [],
      },
    }
    expect(outputVariables(bare, 'noschema')).toEqual(['${noschema.output}'])
  })

  it('给出类型兼容与不兼容提示', () => {
    expect(typeCompatibility('string', 'string')).toBe('compatible')
    expect(typeCompatibility('integer', 'number')).toBe('compatible')
    expect(typeCompatibility('string', 'number')).toBe('incompatible')
    expect(typeCompatibility(undefined, 'number')).toBe('unknown')
  })
})
