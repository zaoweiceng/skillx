import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import { MetricChart } from './MetricChart'
import { compactNumber } from './format'

afterEach(cleanup)
it('converts token magnitudes to K/M/B without losing exact tooltip values', () => {
  expect(compactNumber(500)).toBe('500')
  expect(compactNumber(1200)).toBe('1.2K')
  expect(compactNumber(6369137)).toBe('6.37M')
  expect(compactNumber(1500000000)).toBe('1.5B')
})
it('connects across empty buckets while preserving missing-sample details', () => {
  const points = [1, null, 0].map((value, index) => ({ start: `2026-10-13T0${index}:00:00Z`, end: `2026-10-13T0${index + 1}:00:00Z`, value, samples: value == null ? 0 : 1 }))
  const { container } = render(<MetricChart metric="success" title="任务成功率" points={points} interval={3600} />)
  const line = container.querySelector('path')!
  expect(line.getAttribute('d')!.match(/M /g)).toHaveLength(1)
  expect(line.getAttribute('d')!.match(/L /g)).toHaveLength(1)
  const chart = screen.getByRole('group')
  fireEvent.focus(chart)
  fireEvent.keyDown(chart, { key: 'ArrowRight' })
  expect(screen.getByText('无样本')).toBeInTheDocument()
  fireEvent.keyDown(chart, { key: 'ArrowRight' })
  expect(screen.getAllByText('0.0%').length).toBeGreaterThan(0)
})
