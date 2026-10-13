import { useEffect, useRef, useState } from 'react'
import type { Metric, SeriesPoint } from '@/api/monitoring'
import styles from './MetricChart.module.css'
import { metricLabel, intervalLabel } from './format'

const HEIGHT = 224, LEFT = 64, RIGHT = 16, TOP = 18, BOTTOM = 34
const formatTime = (value: string, daily: boolean) => new Date(value).toLocaleString('zh-CN', daily ? { month: '2-digit', day: '2-digit' } : { hour: '2-digit', minute: '2-digit', hour12: false })

export function MetricChart({ metric, title, points, interval }: { metric: Metric; title: string; points: SeriesPoint[]; interval: number }) {
  const container = useRef<HTMLDivElement>(null)
  const [width, setWidth] = useState(400)
  useEffect(() => {
    if (!container.current) return
    const observer = new ResizeObserver(entries => {
      if (entries[0]?.contentRect.width > 0) setWidth(Math.max(240, entries[0].contentRect.width))
    })
    observer.observe(container.current)
    return () => observer.disconnect()
  }, [])
  const [active, setActive] = useState<number | null>(null)
  const rates = metric === 'success' || metric === 'failure'
  const largest = Math.max(2, ...points.map(point => point.value ?? 0))
  const magnitude = 10 ** Math.floor(Math.log10(largest))
  const maximum = rates ? 1 : Math.ceil(largest / magnitude) * magnitude
  const multiDay = points.length > 0 && +new Date(points.at(-1)!.end) - +new Date(points[0].start) > 86400000
  const x = (index: number) => LEFT + index / Math.max(1, points.length - 1) * (width - LEFT - RIGHT)
  const y = (value: number) => TOP + (1 - value / maximum) * (HEIGHT - TOP - BOTTOM)
  let connected = false
  const path = points.map((point, index) => {
    if (point.value === null) return ''
    const part = `${connected ? 'L' : 'M'} ${x(index)} ${y(point.value)}`
    connected = true
    return part
  }).join(' ')
  const selected = active === null ? undefined : points[active]
  const ticks = [...new Set(Array.from({ length: Math.min(width < 340 ? 3 : 5, points.length) }, (_, index) => Math.round(index * (points.length - 1) / Math.max(1, Math.min(width < 340 ? 3 : 5, points.length) - 1))))]
  return <div ref={container} className={styles.chart} tabIndex={0} role="group" aria-label={`${title}折线图，左右方向键查看各时段`}
    onFocus={() => setActive(0)} onBlur={() => setActive(null)}
    onKeyDown={event => {
      if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
        event.preventDefault()
        setActive(index => Math.max(0, Math.min(points.length - 1, (index ?? 0) + (event.key === 'ArrowLeft' ? -1 : 1))))
      } else if (event.key === 'Escape') setActive(null)
    }} onPointerLeave={() => setActive(null)}>
    <svg viewBox={`0 0 ${width} ${HEIGHT}`} role="img" aria-label={`${title}趋势，每${intervalLabel(interval)}一个数据点`}
      onPointerMove={event => {
        const rect = event.currentTarget.getBoundingClientRect()
        const position = (event.clientX - rect.left) / rect.width * width
        setActive(Math.max(0, Math.min(points.length - 1, Math.round((position - LEFT) / (width - LEFT - RIGHT) * (points.length - 1)))))
      }}>
      {[0, maximum / 2, maximum].map(value => <g key={value}>
        <line x1={LEFT} x2={width - RIGHT} y1={y(value)} y2={y(value)} className={styles.gridLine} />
        <text x={LEFT - 10} y={y(value) + 4} textAnchor="end" className={styles.axis}>{metricLabel(metric, value)}</text>
      </g>)}
      {ticks.map(index => <text key={index} x={x(index)} y={HEIGHT - 10} textAnchor={index === 0 ? 'start' : index === points.length - 1 ? 'end' : 'middle'} className={styles.axis}>{multiDay && interval < 86400 ? <>
        <tspan x={x(index)} dy="-8">{formatTime(points[index].start, true)}</tspan>
        <tspan x={x(index)} dy="13">{formatTime(points[index].start, false)}</tspan>
      </> : formatTime(points[index].start, interval >= 86400)}</text>)}
      <path d={path} className={styles.line} />
      {points.map((point, index) => point.value !== null && <circle key={point.start} cx={x(index)} cy={y(point.value)} r={points.length > 40 ? 1.5 : 2.5} className={styles.dot} />)}
      {selected && <line x1={x(active!)} x2={x(active!)} y1={TOP} y2={HEIGHT - BOTTOM} className={styles.cursor} />}
      {selected?.value != null && <circle cx={x(active!)} cy={y(selected.value)} r={5} className={styles.dot} />}
      {!points.some(point => point.value !== null) && <text x={width / 2} y={HEIGHT / 2} textAnchor="middle" className={styles.axis}>所选时段暂无样本</text>}
    </svg>
    <div className={styles.tooltip} aria-live="polite">
      {selected ? <><span>{new Date(selected.start).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false })} – {formatTime(selected.end, false)}</span><strong>{selected.value === null ? '无样本' : metricLabel(metric, selected.value)}</strong>
        {metric === 'tokens' && selected.value !== null && <span>{new Intl.NumberFormat('zh-CN').format(selected.value)} Token</span>}
      </> : <span>每{intervalLabel(interval)} · 悬停查看详情</span>}
    </div>
  </div>
}
