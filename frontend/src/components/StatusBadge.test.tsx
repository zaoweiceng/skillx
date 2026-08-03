import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { StatusBadge } from '@/components/StatusBadge'

describe('StatusBadge', () => {
  it('将成功状态映射为成功样式', () => {
    render(<StatusBadge status="succeeded" />)

    expect(screen.getByText('succeeded')).toHaveClass('success')
  })

  it('将等待状态显示为可读文本', () => {
    render(<StatusBadge status="waiting_approval" />)

    expect(screen.getByText('waiting approval')).toHaveClass('warning')
  })
})
