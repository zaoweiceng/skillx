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
