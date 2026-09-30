import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { AgentSelector } from './AgentSelector'

const request = vi.fn()
vi.mock('./api', () => ({ runtimeRequest: (...args: unknown[]) => request(...args) }))
afterEach(() => { cleanup(); vi.clearAllMocks(); localStorage.clear() })

describe('Agent 和模型选择', () => {
  it('保存会话模型并在切换 Pi 时清除 TraeX 专用选择', async () => {
    request.mockImplementation(async (path = '', method = 'GET', body?: unknown) => {
      if (method === 'PUT') return body
      if (path === '') return [
        { id: 'traex', name: 'TraeX', kind: 'agent', active: true, isDefault: true, config: { adapter: 'traex' } },
        { id: 'pi', name: 'Pi', kind: 'agent', active: true, config: { adapter: 'pi' } },
      ]
      if (path === '/traex/models') return [{ id: 'model-1', name: '模型一' }]
      return { agentProfileId: 'traex', traexModel: null }
    })
    render(<AgentSelector conversationId="conversation-1" />)
    const model = await screen.findByRole('combobox', { name: 'TraeX 模型' })
    fireEvent.mouseDown(model)
    fireEvent.click(await screen.findByText('模型一'))
    await waitFor(() => expect(request).toHaveBeenCalledWith('/conversations/conversation-1', 'PUT', { agentProfileId: 'traex', traexModel: 'model-1', reasoningEffort: null }))
    expect(JSON.parse(localStorage.getItem('skillx-last-runtime-selection')!)).toMatchObject({ traexModel: 'model-1' })
    fireEvent.mouseDown(screen.getByRole('combobox', { name: 'Agent 执行体' }))
    fireEvent.click(await screen.findByText('Pi'))
    await waitFor(() => expect(request).toHaveBeenCalledWith('/conversations/conversation-1', 'PUT', { agentProfileId: 'pi', traexModel: null, reasoningEffort: null }))
    expect(screen.queryByRole('combobox', { name: 'TraeX 模型' })).not.toBeInTheDocument()
  })

  it('模型列表失败时保留已保存模型并展示错误', async () => {
    localStorage.setItem('skillx-last-runtime-selection', JSON.stringify({agentProfileId: 'traex', traexModel: 'new-model'}))
    request.mockImplementation(async (path = '') => {
      if (path === '') return [{ id: 'traex', name: 'TraeX', kind: 'agent', active: true, isDefault: true, config: { adapter: 'traex' } }]
      if (path === '/traex/models') throw new Error('读取模型超时')
      return { agentProfileId: 'traex', traexModel: 'saved-model' }
    })
    render(<AgentSelector conversationId="conversation-2" />)
    expect(await screen.findByText('读取模型超时')).toBeInTheDocument()
    expect(screen.getByText('saved-model（已选择）')).toBeInTheDocument()
    expect(JSON.parse(localStorage.getItem('skillx-last-runtime-selection')!).traexModel).toBe('new-model')
  })
})

it('selects a Pi model from another service and persists its exact service/model pair', async () => {
  request.mockImplementation(async (path = '', method = 'GET', body?: unknown) => {
    if (method === 'PUT') return body
    if (path === '') return [
      { id: 'pi', name: 'Pi', kind: 'agent', active: true, isDefault: true, config: { adapter: 'pi' } },
      { id: 'service', name: 'My service', kind: 'model', active: true, isDefault: true, config: { model: 'first', models: [{ id: 'first' }, { id: 'second', reasoning: true }] } },
    ]
    return { agentProfileId: null, traexModel: null }
  })
  render(<AgentSelector conversationId="pi-conversation" />)
  fireEvent.mouseDown(await screen.findByRole('combobox', { name: 'Pi 模型' }))
  fireEvent.click(await screen.findByText('second'))
  await waitFor(() => expect(request).toHaveBeenCalledWith('/conversations/pi-conversation', 'PUT', {
    agentProfileId: null, traexModel: null, modelProfileId: 'service', model: 'second', reasoningEffort: null,
  }))
})
