import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ChatComposer } from './ChatComposer'

afterEach(cleanup)

function textFile(name: string, content: string) {
  const file = new File([content], name, {type: 'text/plain'})
  Object.defineProperty(file, 'arrayBuffer', {value: async () => new TextEncoder().encode(content).buffer})
  return file
}

function composer(onSend = vi.fn().mockResolvedValue(true)) {
  render(<ChatComposer input="总结附件" onInputChange={() => {}} disabled={false} sending={false}
    controls={<span>模型</span>} onSend={onSend} onWorkingDirectoryChange={() => {}} />)
  return onSend
}

describe('对话参考文件', () => {
  it('粘贴图片显示缩略图，图片可以单独发送且不作为文本附件', async () => {
    const send = vi.fn().mockResolvedValue(true)
    render(<ChatComposer input="" onInputChange={() => {}} disabled={false} sending={false}
      controls={null} onSend={send} onWorkingDirectoryChange={() => {}} />)
    const file = new File([new Uint8Array([137, 80, 78, 71])], '截图.png', { type: 'image/png' })
    fireEvent.paste(screen.getByLabelText('对话输入'), { clipboardData: { items: [{ kind: 'file', type: 'image/png', getAsFile: () => file }] } })
    expect(await screen.findByRole('img', { name: '截图.png' })).toHaveAttribute('src', 'data:image/png;base64,iVBORw==')
    fireEvent.click(screen.getByRole('button', { name: '发送' }))
    await waitFor(() => expect(send).toHaveBeenCalledWith(expect.anything(), [], undefined,
      [{ name: '截图.png', mimeType: 'image/png', data: 'iVBORw==' }]))
    await waitFor(() => expect(screen.queryByRole('img', { name: '截图.png' })).not.toBeInTheDocument())
  })

  it('选择图片后可移除，发送失败保留预览', async () => {
    const send = composer(vi.fn().mockResolvedValue(false))
    const file = new File(['image'], 'photo.jpg', { type: 'image/jpeg' })
    fireEvent.change(screen.getByLabelText('参考文件'), { target: { files: [file] } })
    await screen.findByRole('img', { name: 'photo.jpg' })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))
    await waitFor(() => expect(send).toHaveBeenCalled())
    expect(screen.getByRole('img', { name: 'photo.jpg' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '删除图片 photo.jpg' }))
    expect(screen.queryByRole('img', { name: 'photo.jpg' })).not.toBeInTheDocument()
  })

  it('连续粘贴图片会排队读取，不丢失先粘贴的图片', async () => {
    composer()
    for (const name of ['first.png', 'second.png']) {
      const file = new File(['image'], name, { type: 'image/png' })
      fireEvent.paste(screen.getByLabelText('对话输入'), { clipboardData: { items: [{ kind: 'file', type: file.type, getAsFile: () => file }] } })
    }
    await screen.findByRole('img', { name: 'first.png' })
    await screen.findByRole('img', { name: 'second.png' })
  })

  it('读取文件正文发送，成功后清空附件', async () => {
    const send = composer()
    fireEvent.change(screen.getByLabelText('参考文件'), {target: {files: [textFile('notes.md', '项目说明')]}})
    await screen.findByText('notes.md')
    fireEvent.click(screen.getByRole('button', {name: '发送'}))
    await waitFor(() => expect(send).toHaveBeenCalledWith(expect.anything(), [{name: 'notes.md', content: '项目说明'}], undefined))
    await waitFor(() => expect(screen.queryByText('notes.md')).not.toBeInTheDocument())
  })

  it('发送失败保留附件，避免重新选择', async () => {
    const send = composer(vi.fn().mockResolvedValue(false))
    fireEvent.change(screen.getByLabelText('参考文件'), {target: {files: [textFile('notes.md', '项目说明')]}})
    await screen.findByText('notes.md')
    fireEvent.click(screen.getByRole('button', {name: '发送'}))
    await waitFor(() => expect(send).toHaveBeenCalled())
    expect(screen.getByText('notes.md')).toBeInTheDocument()
  })

  it('拒绝二进制文件，不把空内容发送给 Agent', async () => {
    composer()
    fireEvent.change(screen.getByLabelText('参考文件'), {target: {files: [textFile('data.bin', 'a\0b')]}})
    expect(await screen.findByText(/是二进制文件/)).toBeInTheDocument()
    expect(screen.queryByText('data.bin')).not.toBeInTheDocument()
  })
})

it('执行时用终止按钮替换发送，保留草稿并在结束后恢复发送', async () => {
  const onStop = vi.fn().mockResolvedValue(undefined)
  const onSend = vi.fn().mockResolvedValue(true)
  const props = { input: '下一条草稿', onInputChange: vi.fn(), disabled: true, sending: false,
    controls: null, onSend, onWorkingDirectoryChange: vi.fn() }
  const { rerender } = render(<ChatComposer {...props} onStop={onStop} />)
  expect(screen.queryByRole('button', { name: '发送' })).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '终止任务' }))
  expect(onStop).toHaveBeenCalledTimes(1)
  expect(onSend).not.toHaveBeenCalled()
  expect(screen.getByLabelText('对话输入')).toHaveValue('下一条草稿')
  rerender(<ChatComposer {...props} onStop={onStop} stopping />)
  expect(screen.getByRole('button', { name: '终止任务' })).toBeDisabled()
  rerender(<ChatComposer {...props} disabled={false} />)
  fireEvent.click(screen.getByRole('button', { name: '发送' }))
  await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1))
})

it('输入为空也可终止，表单提交不会发送或意外终止任务', () => {
  const onStop = vi.fn().mockResolvedValue(undefined)
  const onSend = vi.fn().mockResolvedValue(true)
  render(<ChatComposer input="" onInputChange={vi.fn()} disabled sending={false}
    controls={null} onSend={onSend} onStop={onStop} onWorkingDirectoryChange={vi.fn()} />)
  const stop = screen.getByRole('button', { name: '终止任务' })
  expect(stop).toBeEnabled()
  fireEvent.submit(stop.closest('form')!)
  expect(onStop).not.toHaveBeenCalled()
  expect(onSend).not.toHaveBeenCalled()
  fireEvent.click(stop)
  expect(onStop).toHaveBeenCalledTimes(1)
})

it('Enter sends once; Shift+Enter and IME confirmation preserve the draft', async () => {
  let resolve!: (value: boolean) => void
  const send = composer(vi.fn(() => new Promise<boolean>(done => { resolve = done })))
  const input = screen.getByLabelText('对话输入')
  fireEvent.keyDown(input, { key: 'Enter', shiftKey: true })
  fireEvent.keyDown(input, { key: 'Enter', isComposing: true })
  fireEvent.keyDown(input, { key: 'Enter', keyCode: 229 })
  expect(send).not.toHaveBeenCalled()
  fireEvent.keyDown(input, { key: 'Enter' })
  fireEvent.keyDown(input, { key: 'Enter' })
  expect(send).toHaveBeenCalledTimes(1)
  resolve(true)
  await waitFor(() => expect(send).toHaveBeenCalledTimes(1))
})
