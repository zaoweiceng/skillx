import { cleanup, render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, expect, it } from 'vitest'
import AppShell from './AppShell'

afterEach(cleanup)
it('keeps a single settings icon in the footer and adds Skills to the main navigation', () => {
  render(<MemoryRouter initialEntries={['/settings']}><AppShell /></MemoryRouter>)
  const navigation = screen.getByRole('navigation', {name: '主导航'})
  expect(within(navigation).getByRole('link', {name: 'Skills'})).toHaveAttribute('href', '/skills')
  expect(within(navigation).queryByRole('link', {name: '设置'})).not.toBeInTheDocument()
  const settings = screen.getAllByRole('link', {name: '设置'})
  expect(settings).toHaveLength(1)
  expect(settings[0]).toHaveAttribute('href', '/settings')
  expect(settings[0].textContent).toBe('')
  expect(document.title).toBe('设置 · SkillX 智能工作空间')
})
