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
