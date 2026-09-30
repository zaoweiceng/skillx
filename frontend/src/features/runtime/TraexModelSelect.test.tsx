import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { TraexModelSelect } from './TraexModelSelect'

vi.mock('./api', () => ({ runtimeRequest: vi.fn().mockResolvedValue([
  { id: 'gpt-test', name: 'GPT Test', reasoningEfforts: ['low', 'medium', 'high'], contextWindow: 128000 },
  { id: 'basic', name: 'Basic', reasoningEfforts: [] },
]) }))
afterEach(cleanup)

it('shows reasoning for supported models and sends the canonical medium value', async () => {
  const onChange = vi.fn()
  render(<TraexModelSelect value="gpt-test" onChange={vi.fn()} onReasoningChange={onChange} />)
  fireEvent.mouseDown(await screen.findByRole('combobox', { name: '推理强度' }))
  fireEvent.click(await screen.findByText('中 · Medium'))
  expect(onChange).toHaveBeenCalledWith('medium')
})

it('does not offer reasoning for unsupported models', async () => {
  render(<TraexModelSelect value="basic" onChange={vi.fn()} onReasoningChange={vi.fn()} />)
  await screen.findByText('Basic')
  expect(screen.queryByRole('combobox', { name: '推理强度' })).not.toBeInTheDocument()
})
