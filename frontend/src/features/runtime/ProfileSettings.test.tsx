import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { ProfileSettings } from './ProfileSettings'
const request = vi.fn()
vi.mock('./api', () => ({ runtimeRequest: (...args: unknown[]) => request(...args) }))
afterEach(() => { cleanup(); vi.clearAllMocks() })
it('saves a service with a manual key and multiple models', async () => {
  request.mockResolvedValue([])
  render(<ProfileSettings />)
  fireEvent.click(screen.getByRole('button', { name: '添加配置' }))
  fireEvent.change(screen.getByRole('textbox', { name: '配置名称' }), { target: { value: 'service' } })
  fireEvent.change(screen.getByRole('textbox', { name: 'Base URL' }), { target: { value: 'https://example.com/v1' } })
  fireEvent.change(screen.getByLabelText('API Key'), { target: { value: 'manual-secret' } })
  fireEvent.change(screen.getByRole('textbox', { name: '模型 ID 1' }), { target: { value: 'model-first' } })
  fireEvent.click(screen.getByRole('checkbox', { name: '支持图片 1' }))
  fireEvent.click(screen.getByRole('button', { name: '添加模型' }))
  fireEvent.change(screen.getByRole('textbox', { name: '模型 ID 2' }), { target: { value: 'model-second' } })
  fireEvent.click(screen.getByRole('button', { name: /保\s*存/ }))
  await waitFor(() => expect(request).toHaveBeenCalledWith('', 'POST', expect.objectContaining({
    kind: 'model', config: expect.objectContaining({ apiKey: 'manual-secret', model: 'model-first', models: [expect.objectContaining({ id: 'model-first', images: true }), expect.objectContaining({ id: 'model-second', images: false })] }),
  })))
})
it('editing a service never prepopulates the key and blank retains its reference', async () => {
  request.mockResolvedValue([{ id: 'service', name: 'saved', kind: 'model', active: true, isDefault: true,
    config: { baseUrl: 'https://example.com/v1', model: 'one', models: [{ id: 'one' }], apiKeyEnv: 'PRIVATE_KEY_REFERENCE' } }])
  render(<ProfileSettings />)
  fireEvent.click(await screen.findByRole('button', { name: '编辑' }))
  expect(screen.getByLabelText('API Key')).toHaveValue('')
  fireEvent.click(screen.getByRole('button', { name: /保\s*存/ }))
  await waitFor(() => expect(request).toHaveBeenCalledWith('/service', 'PUT', expect.objectContaining({ config: expect.objectContaining({ apiKeyEnv: 'PRIVATE_KEY_REFERENCE' }) })))
  expect(request.mock.calls.find(call => call[1] === 'PUT')?.[2].config).not.toHaveProperty('apiKey')
})
