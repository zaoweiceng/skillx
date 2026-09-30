import { progressDetail, progressLabel, summarizeProgress, type ProgressStep, type TaskEvent } from '@/components/taskProgressEvents'

/** Keep each model round beside the decision/rejection it produced. */
export function taskTraceSteps(events: TaskEvent[]): ProgressStep[] {
  const steps: ProgressStep[] = []
  let routine: TaskEvent[] = []
  let modelEvents: TaskEvent[] = []
  const flush = () => { steps.push(...summarizeProgress(routine)); routine = [] }
  for (const event of events) {
    if (event.type === 'agent.audit') { modelEvents.push(event); continue }
    const boundary = ['task.decision', 'task.decision_rejected', 'task.failed', 'task.cancelled', 'task.blocked'].includes(event.type)
    if (boundary && modelEvents.length) {
      flush()
      // Preserve the expanded call while a pending round becomes a completed decision.
      steps.push({ id: modelEvents[0].id, createdAt: event.createdAt, label: progressLabel(event) ?? '本轮模型分析',
        detail: progressDetail(event), events: [...modelEvents, event] })
      modelEvents = []
    } else routine.push(event)
  }
  flush()
  if (modelEvents.length) {
    const failed = modelEvents.find(event => event.payload.type === 'agent.failed')
    const finished = modelEvents.some(event => event.payload.type === 'agent.completed')
    const first = modelEvents[0]
    steps.push({ id: first.id, createdAt: first.createdAt,
      label: failed ? '本轮模型调用失败' : finished ? '本轮模型分析已返回' : '正在思考与分析',
      detail: failed ? progressDetail(failed) : null, events: modelEvents })
  }
  return steps
}
