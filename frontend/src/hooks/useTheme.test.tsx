import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { useTheme } from '@/hooks/useTheme'

function ThemeProbe() {
  const { theme, toggleTheme } = useTheme()
  return <button onClick={toggleTheme}>{theme}</button>
}

describe('useTheme', () => {
  beforeEach(() => {
    localStorage.clear()
    document.documentElement.removeAttribute('data-theme')
  })

  afterEach(cleanup)

  it('保存用户选择并应用到根节点', () => {
    localStorage.setItem('skillx-theme', 'light')
    render(<ThemeProbe />)

    expect(document.documentElement.dataset.theme).toBe('light')
    fireEvent.click(screen.getByRole('button', { name: 'light' }))
    expect(document.documentElement.dataset.theme).toBe('dark')
    expect(localStorage.getItem('skillx-theme')).toBe('dark')
  })
})
