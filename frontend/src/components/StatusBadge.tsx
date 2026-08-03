const completed = new Set(['succeeded', 'approved', 'published'])
const active = new Set(['queued', 'ready', 'running', 'retry_waiting'])
const waiting = new Set(['waiting_approval', 'waiting_user', 'pending'])
const failed = new Set(['failed', 'rejected', 'cancelled'])

export function StatusBadge({ status }: { status: string }) {
  let tone = 'neutral'
  if (completed.has(status)) tone = 'success'
  if (active.has(status)) tone = 'active'
  if (waiting.has(status)) tone = 'warning'
  if (failed.has(status)) tone = 'danger'

  return <span className={`status-badge ${tone}`}>{status.split('_').join(' ')}</span>
}
