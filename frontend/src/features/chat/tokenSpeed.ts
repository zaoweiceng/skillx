import type { ConversationTurn } from '@/api/client'

export function tokenSpeedLabel(turn?: ConversationTurn): string | null {
  if (!turn?.completedAt || turn.usage.state !== 'complete') return null
  const stages = turn.usage.stages.filter(stage => (stage.totalTokens ?? 0) > 0)
  if (!stages.length || stages.some(stage => !stage.complete
    || stage.outputTokens == null || !Number.isFinite(stage.outputTokens) || stage.outputTokens < 0
    || stage.durationMs == null || !Number.isFinite(stage.durationMs) || stage.durationMs <= 0)) return null
  const output = stages.reduce((sum, stage) => sum + stage.outputTokens!, 0)
  const duration = stages.reduce((sum, stage) => sum + stage.durationMs!, 0)
  return `${(output * 1000 / duration).toFixed(1)} tokens/s`
}
