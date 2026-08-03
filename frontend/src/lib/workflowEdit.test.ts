import { describe, expect, it } from 'vitest'

import type { WorkflowDefinition } from '@/api/client'
import {
  addNode,
  connectNodes,
  deleteNode,
  disconnectNodes,
  setInputMapping,
  updateNode,
} from '@/lib/workflowEdit'

function baseDefinition(): WorkflowDefinition {
  return {
    apiVersion: 'workflow.platform/v1',
    kind: 'Workflow',
    metadata: { name: 'edit-test' },
    spec: {
      inputSchema: { type: 'object', properties: { name: { type: 'string' } } },
      outputSchema: { type: 'object', properties: { message: { type: 'string' } } },
      nodes: [
        { id: 'input', type: 'input', displayName: '输入' },
        { id: 'output', type: 'output', displayName: '输出' },
      ],
      edges: [],
    },
  }
}

describe('workflowEdit', () => {
  it('新增节点生成唯一 id', () => {
    const { definition, nodeId } = addNode(baseDefinition(), 'transform')
    expect(nodeId).toBe('transform')
    const second = addNode(definition, 'transform')
    expect(second.nodeId).toBe('transform-2')
    expect(second.definition.spec.nodes).toHaveLength(4)
  })

  it('连线成功并写入边', () => {
    const { definition } = addNode(baseDefinition(), 'transform')
    const result = connectNodes(definition, 'input', 'transform')
    expect(result.ok).toBe(true)
    if (result.ok) {
      expect(result.definition.spec.edges).toContainEqual({ source: 'input', target: 'transform' })
    }
  })

  it('拒绝形成环的连线', () => {
    let def = addNode(baseDefinition(), 'transform').definition
    def = addNode(def, 'condition').definition
    def = (connectNodes(def, 'transform', 'condition') as { definition: WorkflowDefinition }).definition
    const result = connectNodes(def, 'condition', 'transform')
    expect(result.ok).toBe(false)
    if (!result.ok) expect(result.error).toContain('环')
  })

  it('拒绝自环、重复边、指向 input、从 output 出发', () => {
    const { definition } = addNode(baseDefinition(), 'transform')
    expect(connectNodes(definition, 'transform', 'transform').ok).toBe(false)
    expect(connectNodes(definition, 'transform', 'input').ok).toBe(false)
    expect(connectNodes(definition, 'output', 'transform').ok).toBe(false)
    const once = connectNodes(definition, 'input', 'transform')
    expect(once.ok).toBe(true)
    if (once.ok) {
      expect(connectNodes(once.definition, 'input', 'transform').ok).toBe(false)
    }
  })

  it('删除节点同时删除相关连线，禁止删除 input/output', () => {
    let def = addNode(baseDefinition(), 'transform').definition
    def = (connectNodes(def, 'input', 'transform') as { definition: WorkflowDefinition }).definition
    def = (connectNodes(def, 'transform', 'output') as { definition: WorkflowDefinition }).definition
    const result = deleteNode(def, 'transform')
    expect(result.ok).toBe(true)
    if (result.ok) {
      expect(result.definition.spec.nodes.find((n) => n.id === 'transform')).toBeUndefined()
      expect(result.definition.spec.edges).toHaveLength(0)
    }
    expect(deleteNode(def, 'input').ok).toBe(false)
    expect(deleteNode(def, 'output').ok).toBe(false)
  })

  it('断开连线', () => {
    let def = addNode(baseDefinition(), 'transform').definition
    def = (connectNodes(def, 'input', 'transform') as { definition: WorkflowDefinition }).definition
    const after = disconnectNodes(def, 'input', 'transform')
    expect(after.spec.edges).toHaveLength(0)
  })

  it('更新节点字段与输入映射', () => {
    const { definition } = addNode(baseDefinition(), 'transform')
    const renamed = updateNode(definition, 'transform', { displayName: '格式化', sideEffect: true })
    const node = renamed.spec.nodes.find((n) => n.id === 'transform')
    expect(node?.displayName).toBe('格式化')
    expect(node?.sideEffect).toBe(true)
    const mapped = setInputMapping(renamed, 'transform', 'name', '${input.name}')
    expect(
      mapped.spec.nodes.find((n) => n.id === 'transform')?.input?.name,
    ).toBe('${input.name}')
  })
})
