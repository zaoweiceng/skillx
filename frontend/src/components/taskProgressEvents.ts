export type TaskEvent = { id: number; type: string; payload: Record<string, unknown>; createdAt: string }
const labels: Record<string, string> = {
  'learning.published': '已固化为工作流', 'learning.blocked': '固化受阻', 'learning.queued': '等待后台固化', 'learning.skipped': '无需固化',
  'task.decision_rejected': '模型返回格式不符合要求，正在重试',
  'task.requeued': '服务重启，任务已重新排队',
  'task.accepted': '任务已接受', 'task.succeeded': '任务完成', 'task.failed': '任务失败',
  'task.cancelled': '任务已取消', 'task.blocked': '任务受阻',
  'invocation.queued': '调用已排队', 'invocation.succeeded': '调用完成',
  'invocation.failed': '调用失败', 'human.requested': '等待你的确认',
  'human.answered': '已收到你的回复',
  'model.request.dispatched': '请求已发送，等待模型响应',
  'agent.step.completed': '模型响应已返回', 'agent.completed': '本轮模型处理完成',
  'tool.call.started': '正在调用工具', 'tool.call.completed': '工具执行完成', 'tool.result': '工具结果已返回', 'agent.failed': '模型调用失败',
  'tool.requested': '正在调用工具', 'tool.completed': '工具执行完成',
}
export const kindLabels: Record<string, string> = { code: '代码', model: '模型', tool: '工具', capability: '能力', workflow: '工作流', skillx: 'SkillX', install: '依赖安装' }
export function progressLabel(event: TaskEvent): string | null {
  if (event.type === 'task.decision') return typeof event.payload.summary === 'string' ? event.payload.summary : '已确定下一步操作'
  if (event.type === 'agent.audit') {
    // Only surface execution metadata, never raw model reasoning or structured output fragments.
    return labels[String(event.payload.type)] ?? null
  }
  if (event.type === 'invocation.queued') return `${kindLabels[String(event.payload.kind)] ?? '能力'}调用已排队`
  return labels[event.type] ?? null
}


export function progressDetail(event: TaskEvent): string | null {
  const payload = event.payload
  if (event.type === 'task.decision_rejected') return '模型已响应，但结果未通过任务契约校验；系统将携带校验反馈重试。'
  if (event.type === 'task.accepted') return null
  if (event.type === 'task.decision') {
    const remaining = payload.remaining
    return Array.isArray(remaining) && remaining.length ? `接下来：${remaining.join('；')}` : null
  }
  if (event.type === 'agent.audit') {
    const data = (payload.payload ?? {}) as Record<string, unknown>
    switch (payload.type) {
      case 'model.request.dispatched': return '正在等待模型返回下一步操作或回答；这条事件不代表已经读取文件或执行命令。'
      case 'agent.step.completed': return `本轮输入 ${data.inputTokens ?? 0} Token，输出 ${data.outputTokens ?? 0} Token。`
      case 'agent.completed': return '模型响应已返回，后续实际操作以工具调用记录为准。'
      case 'tool.requested': case 'tool.call.started': return `工具：${data.capabilityRef ?? data.toolName ?? data.name ?? '见调用参数'}`
      case 'tool.result': case 'tool.call.completed': return `执行状态：${data.status ?? '已返回'}，可展开查看结果。`
      case 'agent.failed': return typeof data.message === 'string' ? data.message : '模型调用失败'
    }
  }
  if (event.type === 'invocation.queued') {
    const args = (payload.request ?? {}) as Record<string, unknown>
    if (typeof payload.reason === 'string' && payload.reason) return payload.reason
    if (payload.kind === 'code') return `执行 ${args.language ?? ''} 脚本；具体代码和输出可在调用记录中展开。`
    if (args.capabilityVersionId) return `能力版本：${args.capabilityVersionId}\n接口：${args.interface ?? 'execute'}`
    if (args.capabilityRef) return `调用工具：${args.capabilityRef}`
    if (payload.kind === 'model') return '调用模型处理本次输入。'
    return '调用已提交，尚不代表执行成功。'
  }
  if (event.type === 'invocation.succeeded') {
    const output = payload.output as Record<string, unknown> | undefined
    if (typeof output?.stdout === 'string') return `脚本退出码：${output.exitCode ?? '未知'}。已返回 ${output.stdout.length} 字符输出，请在调用记录中查看结果摘要或完整内容。`
    return output ? JSON.stringify(output, null, 2) : '调用已成功完成。'
  }
  if (event.type === 'invocation.failed' || event.type === 'task.failed') {
    const error = payload.error as Record<string, unknown> | undefined
    return typeof error?.message === 'string' ? error.message : '执行失败，请查看错误详情。'
  }
  if (event.type === 'task.cancelled') return '任务已取消，后续操作不会继续派发。'
  return null
}

export type ProgressStep = { id: number; createdAt: string; label: string; detail: string | null; events: TaskEvent[] }

/** Keep decisions and failures readable; collapse transport chatter between them. */
export function summarizeProgress(events: TaskEvent[]): ProgressStep[] {
  const steps: ProgressStep[] = []
  let batch: TaskEvent[] = []
  function flush() {
    if (!batch.length) return
    const completed = batch.filter(event => event.type === 'invocation.succeeded').length
    const queued = batch.filter(event => event.type === 'invocation.queued').length
    if (completed || queued) {
      const purposes = [...new Set(batch.filter(event => event.type === 'invocation.queued')
        .map(event => event.payload.reason).filter((reason): reason is string => typeof reason === 'string' && !!reason))]
      steps.push({ id: batch[0].id, createdAt: batch.at(-1)!.createdAt,
        label: completed ? (queued > completed ? `${queued} 项操作 · ${completed} 项已完成` : `已完成 ${completed} 项操作`) : `已安排 ${queued} 项操作`,
        detail: purposes.length ? purposes.join('；') : null, events: batch })
    }
    batch = []
  }
  for (const event of events) {
    const label = progressLabel(event)
    if (!label || event.type === 'task.activity') continue
    const routine = ['invocation.queued', 'invocation.succeeded'].includes(event.type)
      || (event.type === 'agent.audit' && event.payload.type !== 'agent.failed')
    if (routine) { batch.push(event); continue }
    flush()
    steps.push({ id: event.id, createdAt: event.createdAt, label, detail: progressDetail(event), events: [event] })
  }
  flush()
  return steps
}
