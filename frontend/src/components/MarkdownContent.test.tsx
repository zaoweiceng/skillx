import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import MarkdownContent from './MarkdownContent'
const diagram = vi.hoisted(() => ({ initialize: vi.fn(), render: vi.fn() }))
vi.mock('mermaid', () => ({ default: diagram }))
afterEach(() => { cleanup(); vi.clearAllMocks() })
it('renders headings, GFM tables, code and math without executing HTML', () => {
  const { container } = render(<MarkdownContent>{'## 分析结果\n\n**结论**与 `code`\n\n| 输入 | 结果 |\n| --- | --- |\n| x | y |\n\n行内 $E=mc^2$\n\n$$\n\\sum_{i=1}^{n} i=\\frac{n(n+1)}{2}\n$$\n\n<script>alert(1)</script>\n\n[危险](javascript:alert(1))'}</MarkdownContent>)
  expect(screen.getByRole('heading', { name: '分析结果', level: 2 })).toBeTruthy()
  expect(screen.getByRole('table')).toBeTruthy()
  expect(container.querySelectorAll('.katex').length).toBe(2)
  expect(container.querySelector('.katex-display')).toBeTruthy()
  expect(container.querySelector('script')).toBeNull()
  expect(container.querySelector('a')?.getAttribute('href') ?? '').not.toContain('javascript:')
})
it('renders mermaid as a sanitized diagram rather than plain code', async () => {
  diagram.render.mockResolvedValue({ svg: '<svg xmlns="http://www.w3.org/2000/svg"><text>开始</text><script>alert(1)</script></svg>' })
  const { container } = render(<MarkdownContent>{'```mermaid\nflowchart LR\n A[开始] --> B[完成]\n```'}</MarkdownContent>)
  await screen.findByRole('img', { name: 'Mermaid 流程图' })
  expect(container.querySelector('svg text')?.textContent).toBe('开始')
  expect(container.querySelector('script')).toBeNull()
  expect(diagram.initialize).toHaveBeenCalledWith(expect.objectContaining({ securityLevel: 'strict' }))
})
it('keeps invalid diagram source available and clears stale output on updates', async () => {
  diagram.render.mockRejectedValue(new Error('parse error'))
  const view = render(<MarkdownContent>{'```mermaid\ninvalid diagram\n```'}</MarkdownContent>)
  await waitFor(() => expect(screen.getByRole('status').textContent).toContain('语法有误'))
  expect(screen.getByText('invalid diagram')).toBeTruthy()
  diagram.render.mockResolvedValue({ svg: '<svg><text>恢复</text></svg>' })
  view.rerender(<MarkdownContent>{'```mermaid\nflowchart LR\n A --> B\n```'}</MarkdownContent>)
  await screen.findByRole('img', { name: 'Mermaid 流程图' })
  expect(screen.queryByText(/语法有误/)).toBeNull()
})
