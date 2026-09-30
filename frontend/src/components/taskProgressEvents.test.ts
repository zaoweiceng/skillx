import { expect, it } from 'vitest'
import { summarizeProgress, progressDetail, progressLabel, type TaskEvent } from './taskProgressEvents'

it('summarizes a batch without losing decisions, failures or original events', () => {
  const event = (id: number, type: string, payload = {}): TaskEvent => ({ id, type, payload, createdAt: '2026-09-30T00:00:00Z' })
  const events = [event(1, 'task.decision', { summary: '读取入口文件' }),
    ...Array.from({ length: 6 }, (_, i) => event(i + 2, 'invocation.queued', { kind: 'tool', reason: '读取模块配置' })),
    event(8, 'agent.audit', { type: 'agent.step.completed' }),
    event(9, 'invocation.succeeded'), event(10, 'invocation.failed', { error: { message: '路径不存在' } }),
    event(11, 'task.decision', { summary: '调整读取路径' })]
  const steps = summarizeProgress(events)
  expect(steps.map(step => step.label)).toEqual(['读取入口文件', '6 项操作 · 1 项已完成', '调用失败', '调整读取路径'])
  expect(steps[1].events).toHaveLength(8)
  expect(steps[1].detail).toBe('读取模块配置')
  expect(steps[2].detail).toBe('路径不存在')
})

it('omits transport-only chatter from the default timeline', () => {
  expect(summarizeProgress([{ id: 1, type: 'agent.audit', payload: { type: 'model.request.dispatched' }, createdAt: '' }])).toEqual([])
})

it('does not repeat the user goal in accepted progress', () => {
  expect(progressDetail({ id: 1, type: 'task.accepted', payload: { goal: '完整问题' }, createdAt: '' })).toBeNull()
})

it('reports Skill and SkillX publication without claiming the whole task became a workflow', () => {
  const event: TaskEvent = {id: 1, type: 'learning.published', createdAt: '', payload: {
    skills: {'分析方法': 'guide-id'}, skillx: {'数值变换': 'version-id'}, reason: '排除一次性结论',
  }}
  const steps = summarizeProgress([event])
  expect(steps[0].label).toBe('已固化 1 个 Skill · 1 个 SkillX')
  expect(steps[0].detail).toBe('排除一次性结论')
  expect(steps[0].label).not.toContain('工作流')
})

it('labels specialist stages and leaves learning audit chatter out of foreground steps', () => {
  const event: TaskEvent = {id: 1, type: 'learning.agent.validation_failed', createdAt: '', payload: {role: 'skillx', name: 'format', error: '契约验证失败'}}
  expect(progressLabel(event)).toBe('SkillX 构建 Agent · 验证失败，等待修订 · format')
  expect(progressDetail(event)).toBe('契约验证失败')
  expect(summarizeProgress([{...event, type: 'learning.agent.audit'}])).toEqual([])
})
