import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { TaskTraceTurns } from './TaskTraceTurns'
import type { Message } from './useConversation'
vi.mock('./TaskTrace', () => ({ TaskTrace: ({ id }: { id: string }) => <div data-testid="trace">{id}</div> }))
afterEach(cleanup)
const turn = (id: string, text: string): Message[] => [
  { id: `u-${id}`, role: 'user', content: { type: 'text', text } },
  { id: `a-${id}`, role: 'assistant', content: { type: 'task', text: '', taskId: id } },
]
const messages = [...turn('first', '分析失败日志'), ...turn('second', '解释环境变量')]
it('defaults to latest and switches to a previous round using its prompt', () => {
  render(<TaskTraceTurns messages={messages} open />)
  expect(screen.getByTestId('trace')).toHaveTextContent('second')
  expect(screen.getByRole('option', { name: '第 1 轮 · 分析失败日志' })).toBeInTheDocument()
  fireEvent.change(screen.getByRole('combobox'), { target: { value: 'first' } })
  expect(screen.getByTestId('trace')).toHaveTextContent('first')
})
it('keeps historical selection across polling, new turns and closing the sidebar', () => {
  const view = render(<TaskTraceTurns messages={messages} open />)
  fireEvent.change(screen.getByRole('combobox'), { target: { value: 'first' } })
  const next = [...messages, ...turn('third', '继续检查')]
  view.rerender(<TaskTraceTurns messages={next} open={false} />)
  expect(screen.queryByTestId('trace')).not.toBeInTheDocument()
  view.rerender(<TaskTraceTurns messages={next} open />)
  expect(screen.getByTestId('trace')).toHaveTextContent('first')
  fireEvent.change(screen.getByRole('combobox'), { target: { value: 'third' } })
  view.rerender(<TaskTraceTurns messages={[...next, ...turn('fourth', '再试一次')]} open />)
  expect(screen.getByTestId('trace')).toHaveTextContent('fourth')
})
it('resets to latest when a different conversation mounts and deduplicates task messages', () => {
  const view = render(<TaskTraceTurns key="a" messages={[...messages, messages[3]]} open />)
  expect(screen.getAllByRole('option')).toHaveLength(2)
  fireEvent.change(screen.getByRole('combobox'), { target: { value: 'first' } })
  view.rerender(<TaskTraceTurns key="b" messages={turn('other', '其他对话')} open />)
  expect(screen.getByTestId('trace')).toHaveTextContent('other')
})
