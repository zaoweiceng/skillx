import { expect, it } from 'vitest'
import { taskTraceSteps } from './taskTraceSteps'
import { collectModelCalls } from '@/components/llmTraceData'
import type { TaskEvent } from '@/components/taskProgressEvents'
const event = (id: number, type: string, payload = {}): TaskEvent => ({ id, type, payload, createdAt: '2026-09-30T00:00:00Z' })
const audit = (id: number, requestId: string) => event(id, 'agent.audit', { type: 'model.request.dispatched', payload: { requestId, model: 'test-model' } })
it('associates each model call with its own decision across intervening operations', () => {
  const steps = taskTraceSteps([
    event(1, 'task.accepted'), audit(2, 'first'),
    event(3, 'agent.audit', { type: 'model.trace', payload: { requestId: 'first', phase: 'response', chunkCount: 1, chunkIndex: 0, data: '{}' } }),
    event(4, 'task.decision', { summary: '读取入口文件' }),
    event(5, 'invocation.queued'), event(6, 'invocation.succeeded'),
    audit(7, 'second'), event(8, 'task.decision', { summary: '验证读取结果' }),
  ])
  expect(collectModelCalls(steps.find(step => step.label === '读取入口文件')!.events).map(call => call.id)).toEqual(['first'])
  expect(collectModelCalls(steps.find(step => step.label === '验证读取结果')!.events).map(call => call.id)).toEqual(['second'])
  expect(steps.find(step => step.label === '已完成 1 项操作')!.events.map(item => item.id)).toEqual([5, 6])
})
it('retains in-flight calls and binds failed calls to the rejection stage', () => {
  const pending = taskTraceSteps([audit(1, 'pending')])
  expect(pending[0].label).toBe('正在思考与分析')
  expect(collectModelCalls(pending[0].events)[0].id).toBe('pending')
  const rejected = taskTraceSteps([audit(1, 'bad'), event(2, 'task.decision_rejected')])
  expect(collectModelCalls(rejected[0].events)[0].id).toBe('bad')
  expect(rejected[0].id).toBe(pending[0].id)
  expect(rejected).toHaveLength(1)
})
