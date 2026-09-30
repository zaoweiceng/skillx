import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import CapabilityDetailPage from './CapabilityDetailPage'
import WorkflowExecutePage from './WorkflowExecutePage'

const request = vi.fn()
vi.mock('@/api/http', () => ({ request: (...args: unknown[]) => request(...args) }))

afterEach(() => { cleanup(); vi.clearAllMocks() })

describe('统一能力执行面板', () => {
  it('开放对象可添加属性并作为对象提交', async () => {
    request.mockImplementation(async (path: string) => {
      if (path.endsWith('/versions')) return [{id: 'v', version: '1', interfaces: {execute: {
        inputSchema: {type: 'object', properties: {formValues: {type: 'object', title: '表单值'}}},
      }}}]
      if (path.startsWith('/invocations?')) return []
      return {id: 'call', status: 'queued'}
    })
    render(<MemoryRouter initialEntries={['/capabilities/example']}><Routes>
      <Route path="/capabilities/:id" element={<CapabilityDetailPage />} />
    </Routes></MemoryRouter>)
    fireEvent.click(await screen.findByRole('button', {name: '添加一项'}))
    const fields = screen.getAllByRole('textbox')
    fireEvent.change(fields[0], {target: {value: 'purpose'}})
    fireEvent.blur(fields[0])
    fireEvent.change(screen.getAllByRole('textbox')[1], {target: {value: '差旅'}})
    fireEvent.click(screen.getByRole('button', {name: '运行能力'}))
    await waitFor(() => expect(request).toHaveBeenCalledWith('/capability-versions/v/invocations', expect.objectContaining({method: 'POST'})))
    const call = request.mock.calls.find(([url]) => url === '/capability-versions/v/invocations')
    expect(JSON.parse(call![1].body).input).toEqual({formValues: {purpose: '差旅'}})
  })
  it.each(['capabilities', 'workflows'])('%s 入口使用相同契约和调用协议', async route => {
    request.mockImplementation(async (path: string) => {
      if (path === '/capabilities/example/versions') return [{
        id: 'locked-version', version: '1', name: 'greeting', displayName: '问候生成', description: '根据姓名生成问候语', interfaces: {execute: {
          inputSchema: {type: 'object', required: ['name'], properties: {name: {type: 'string', title: '姓名'}}},
          outputSchema: {type: 'object'}, uiSchema: {name: {'ui:placeholder': '输入姓名'}},
        }},
      }]
      if (path.startsWith('/invocations?')) return []
      return {id: 'invocation', status: 'queued'}
    })
    const path = route === 'capabilities' ? '/capabilities/example' : '/workflows/example/run'
    render(<MemoryRouter initialEntries={[path]}><Routes>
      <Route path="/capabilities/:id" element={<CapabilityDetailPage />} />
      <Route path="/workflows/:id/run" element={<WorkflowExecutePage />} />
    </Routes></MemoryRouter>)
    fireEvent.change(await screen.findByPlaceholderText('输入姓名'), {target: {value: 'Ada'}})
    fireEvent.click(screen.getByRole('button', {name: '运行能力'}))
    await waitFor(() => expect(request).toHaveBeenCalledWith(
      '/capability-versions/locked-version/invocations', expect.objectContaining({method: 'POST'}),
    ))
    const call = request.mock.calls.find(([url]) => url.endsWith('/invocations') && !url.includes('?'))
    expect(JSON.parse(call![1].body)).toEqual({interface: 'execute', input: {name: 'Ada'}, idempotencyKey: expect.any(String)})
    expect(screen.getByRole('heading', {name: '问候生成'})).toBeInTheDocument()
    expect(screen.getByText('根据姓名生成问候语')).toBeInTheDocument()
  })

  it('同一历史条目展示详情并通过 Invocation 取消', async () => {
    request.mockImplementation(async (path: string) => {
      if (path.endsWith('/versions')) return [{id: 'v', version: '1', interfaces: {execute: {inputSchema: {type: 'object'}}}}]
      if (path.startsWith('/invocations?')) return [{id: 'call', status: 'waiting_execution', runId: 'run', interactions: []}]
      return {id: 'call', status: 'cancelled'}
    })
    render(<MemoryRouter initialEntries={['/capabilities/example']}><Routes>
      <Route path="/capabilities/:id" element={<CapabilityDetailPage />} />
    </Routes></MemoryRouter>)
    expect(await screen.findByRole('link', {name: '运行详情'})).toHaveAttribute('href', '/runs/run')
    fireEvent.click(screen.getByRole('button', {name: /取\s*消/}))
    await waitFor(() => expect(request).toHaveBeenCalledWith('/invocations/call/cancel', {method: 'POST'}))
  })
})
