import { expect, it } from 'vitest'
import type { ConversationTurn } from '@/api/client'
import { tokenSpeedLabel } from './tokenSpeed'

const turn: ConversationTurn = {
  id: 'turn', status: 'completed', workflowModeSnapshot: 'enabled',
  acceptedAt: '2026-10-13T00:00:00Z', firstResponseAt: null,
  completedAt: '2026-10-13T00:01:00Z', elapsedMs: 60000,
  usage: { state: 'complete', totalTokens: 10000, stages: [
    { stage: 'task_decision', provider: 'pi', model: 'test', complete: true,
      totalTokens: 5000, outputTokens: 100, durationMs: 1000 },
    { stage: 'task_decision', provider: 'pi', model: 'test', complete: true,
      totalTokens: 5000, outputTokens: 300, durationMs: 9000 },
  ] },
}
it('uses weighted model output speed, excluding input tokens and task/tool time', () => {
  expect(tokenSpeedLabel(turn)).toBe('40.0 tokens/s')
})
it('hides speed during execution and when measurements are missing or invalid', () => {
  expect(tokenSpeedLabel({ ...turn, completedAt: null })).toBeNull()
  expect(tokenSpeedLabel({ ...turn, usage: { ...turn.usage, state: 'partial' } })).toBeNull()
  for (const durationMs of [null, 0, -1, Infinity]) {
    expect(tokenSpeedLabel({ ...turn, usage: { ...turn.usage,
      stages: [{ ...turn.usage.stages[0], durationMs }] } })).toBeNull()
  }
  expect(tokenSpeedLabel({ ...turn, usage: { ...turn.usage,
    stages: [{ ...turn.usage.stages[0], outputTokens: null }] } })).toBeNull()
})
