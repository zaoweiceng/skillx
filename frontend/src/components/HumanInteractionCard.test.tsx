import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import HumanInteractionCard, { type HumanInteraction } from './HumanInteractionCard'
import { request } from '@/api/http'
vi.mock('@/api/http', () => ({ request: vi.fn() }))
const item: HumanInteraction = { id: 'question', kind: 'authorization', status: 'pending', revision: 3, snapshotDigest: 'digest', request: { title: '安装依赖', reason: '需要新增工具', inputSchema: {}, recommended: '批准' } }
beforeEach(() => vi.clearAllMocks())
afterEach(cleanup)
describe('HumanInteractionCard', () => {
  it('推荐项不会自动提交，拒绝携带版本与快照', async () => {
    vi.mocked(request).mockResolvedValue({})
    const answered = vi.fn()
    render(<HumanInteractionCard item={item} onAnswered={answered} />)
    expect(request).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: /拒\s*绝/ }))
    await waitFor(() => expect(answered).toHaveBeenCalledOnce())
    const body = JSON.parse(vi.mocked(request).mock.calls[0][1]!.body as string)
    expect(body).toMatchObject({ revision: 3, snapshotDigest: 'digest', answer: { approved: false } })
    expect(body.idempotencyKey).toBeTruthy()
  })
  it('已失效卡片不能再次授权', () => {
    render(<HumanInteractionCard item={{...item, status: 'cancelled'}} onAnswered={vi.fn()} />)
    expect(screen.queryByText('批准本次操作')).not.toBeInTheDocument()
    expect(request).not.toHaveBeenCalled()
  })
})

it('已回答的选择展示表单标签与补充说明，不显示内部值、JSON和未选项', () => {
  const { container } = render(<HumanInteractionCard item={{ ...item, kind: 'choice', status: 'answered',
    request: { title: '选择创建渠道', reason: '很长的执行背景',
      inputSchema: { properties: { channel: { title: '创建渠道', enum: ['manual_link', 'token'] }, note: { title: '补充说明' } } },
      uiSchema: { channel: { 'ui:enumNames': ['我手动创建', '提供凭据'] } },
      options: [{value: 'manual_link', label: '手动创建'}, {value: 'token', label: '提供凭据'}] },
    answer: { channel: 'manual_link', note: '请提供标题和说明' },
  }} onAnswered={vi.fn()} />)
  expect(screen.getByText('我手动创建')).toBeInTheDocument()
  expect(screen.getByText('请提供标题和说明')).toBeInTheDocument()
  expect(screen.queryByText('很长的执行背景')).not.toBeInTheDocument()
  expect(screen.queryByText('提供凭据')).not.toBeInTheDocument()
  expect(container.querySelector('pre')).toBeNull()
  expect(container).not.toHaveTextContent('manual_link')
})

it('提交成功立即显示授权结果，不必等待轮询', async () => {
  vi.mocked(request).mockResolvedValue({})
  render(<HumanInteractionCard item={item} onAnswered={vi.fn()} />)
  fireEvent.click(screen.getByRole('button', {name: '批准本次操作'}))
  expect(await screen.findByText('已批准本次操作')).toBeInTheDocument()
  expect(screen.queryByRole('button', {name: '批准本次操作'})).not.toBeInTheDocument()
})

it('兼容选项值标签、数组和嵌套回答', () => {
  render(<HumanInteractionCard item={{...item, status: 'answered', request: { ...item.request,
    inputSchema: { properties: { choices: {title: '选择项', type: 'array', items: {oneOf: [{const: 'a', title: '选项甲'}]}}, details: {title: '详情', properties: {note: {title: '说明'}}} } },
    options: [{value: 'b', label: '选项乙'}] },
    answer: {choices: ['a', 'b'], details: {note: '保留完整内容'}},
  }} onAnswered={vi.fn()} />)
  expect(screen.getByText('选项甲')).toBeInTheDocument()
  expect(screen.getByText('选项乙')).toBeInTheDocument()
  expect(screen.getByText('保留完整内容')).toBeInTheDocument()
})
