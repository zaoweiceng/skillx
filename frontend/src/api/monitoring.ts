import { request } from './http'
export type Metric = 'tokens' | 'duration' | 'success' | 'skillx' | 'workflow' | 'failure' | 'reasons'
export type Range = { start: string; end: string }
export type SeriesPoint = { start: string; end: string; value: number | null; samples: number }
export type DurationRank = { id: string; goal: string; status: string; conversationId: string | null; createdAt: string; durationMs: number }
export type MetricResult = { ranking?: DurationRank[]; series?: SeriesPoint[]; intervalSeconds?: number; value: number | null; basis: string; tasks?: number; completed?: number; cancelled?: number; incompleteTasks?: number; samples?: number; items?: { reason: string; count: number; example?: string }[] }
export type Session = { id: string; runId: string; createdAt: string; cwd: string; bytes: number }
export type RecordItem = { timestamp: string; type: string; payload: unknown }
export const monitoring = {
  metric: (key: Metric, range: Range, signal: AbortSignal) => request<MetricResult>(`/monitoring/metrics/${key}?${new URLSearchParams({ ...range, timezone_offset: String(-new Date().getTimezoneOffset()) })}`, { signal }),
  sessions: (range: Range, offset: number, signal: AbortSignal) => request<{ items: Session[]; total: number }>(`/monitoring/sessions?${new URLSearchParams(range)}&offset=${offset}`, { signal }),
  records: (id: string, offset: number, signal: AbortSignal) => request<{ items: RecordItem[]; nextOffset: number | null }>(`/monitoring/sessions/${id}?offset=${offset}`, { signal }),
}
