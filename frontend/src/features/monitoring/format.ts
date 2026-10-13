import type { Metric } from '@/api/monitoring'

export function compactNumber(value: number): string {
  const unit = Math.abs(value) >= 999_500_000 ? 1e9 : Math.abs(value) >= 999_500 ? 1e6 : Math.abs(value) >= 1000 ? 1e3 : 1
  return `${new Intl.NumberFormat('en-US', { maximumFractionDigits: unit === 1 ? 0 : 2 }).format(value / unit)}${unit === 1e9 ? 'B' : unit === 1e6 ? 'M' : unit === 1e3 ? 'K' : ''}`
}

export function metricLabel(metric: Metric, value: number | null): string {
  if (value === null) return '—'
  if (metric === 'success' || metric === 'failure') return `${(value * 100).toFixed(1)}%`
  if (metric === 'duration') return value >= 3600000 ? `${(value / 3600000).toFixed(1)} 小时` : value >= 60000 ? `${(value / 60000).toFixed(1)} 分钟` : `${(value / 1000).toFixed(1)} 秒`
  return compactNumber(value)
}

export function intervalLabel(seconds: number) {
  return seconds < 3600 ? `${seconds / 60} 分钟` : seconds < 86400 ? `${seconds / 3600} 小时` : `${seconds / 86400} 天`
}
