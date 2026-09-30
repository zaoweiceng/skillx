import { Alert, Button, Flex, Input, Tag, Tooltip } from 'antd'
import { ArrowUp, FileText, FolderOpen, Paperclip } from 'lucide-react'
import { useRef, useState, type FormEvent, type ReactNode } from 'react'
import type { ChatReferenceFile } from '@/api/client'
import styles from './chat.module.css'
import { DirectoryPicker } from './DirectoryPicker'

const MAX_FILES = 8
const MAX_FILE_BYTES = 128 * 1024
const MAX_TOTAL_BYTES = 256 * 1024

export function ChatComposer({ input, onInputChange, disabled, sending, controls, onSend, directoryControl, workingDirectory, onWorkingDirectoryChange }: {
  workingDirectory?: string
  onWorkingDirectoryChange: (path: string | undefined) => void
  input: string
  onInputChange: (value: string) => void
  disabled: boolean
  sending: boolean
  controls: ReactNode
  directoryControl?: ReactNode
  onSend: (event: FormEvent, files: ChatReferenceFile[], workingDirectory?: string) => Promise<boolean>
}) {
  const fileInput = useRef<HTMLInputElement>(null)
  const [files, setFiles] = useState<ChatReferenceFile[]>([])
  const [error, setError] = useState('')
  const [reading, setReading] = useState(false)

  async function addFiles(selected: File[]) {
    setError('')
    setReading(true)
    try {
      if (files.length + selected.length > MAX_FILES) throw new Error(`最多选择 ${MAX_FILES} 个参考文件`)
      const incoming = await Promise.all(selected.map(async file => {
        if (file.size > MAX_FILE_BYTES) throw new Error(`${file.name} 超过 128 KB，请选择较小的文件`)
        let content: string
        try {
          content = new TextDecoder('utf-8', {fatal: true}).decode(await file.arrayBuffer())
        } catch {
          throw new Error(`${file.name} 不是 UTF-8 文本文件，请转换为文本后添加`)
        }
        if (content.includes('\0')) throw new Error(`${file.name} 是二进制文件，请选择文本、代码或 Markdown 文件`)
        return {name: file.name, content}
      }))
      const next = [...files, ...incoming]
      if (new TextEncoder().encode(next.map(file => file.content).join('')).length > MAX_TOTAL_BYTES) {
        throw new Error('参考文件总大小不能超过 256 KB')
      }
      setFiles(next)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '文件读取失败')
    } finally {
      setReading(false)
      if (fileInput.current) fileInput.current.value = ''
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (disabled || reading || !input.trim()) return
    setError('')
    if (await onSend(event, files, workingDirectory)) setFiles([])
  }

  return <form className={styles.composer} onSubmit={submit}>
    {(workingDirectory || files.length > 0) && <Flex gap={6} wrap className={styles.attachments}>
      {workingDirectory && <Tag className={styles.directoryTag} icon={<FolderOpen size={13} />} closable={!sending}
      onClose={event => { event.preventDefault(); onWorkingDirectoryChange(undefined) }} title={workingDirectory}>
        <span className={styles.directoryPath}>{workingDirectory}</span>
      </Tag>}
      {files.map((file, index) => <Tag key={`${file.name}-${index}`} icon={<FileText size={13} />}
        closable={!sending} onClose={() => setFiles(current => current.filter((_, i) => i !== index))}>
        {file.name}
      </Tag>)}
    </Flex>}
    <Input.TextArea variant="borderless" aria-label="对话输入" placeholder="描述一个需要探索或固化的任务..."
      value={input} onChange={event => onInputChange(event.target.value)} autoSize={{minRows: 2, maxRows: 8}} />
    <div className={styles.composerToolbar}>
      <div className={styles.composerOptions}>{controls}</div>
      <Flex gap={8} align="center" className={styles.composerActions}>
        {directoryControl ?? <DirectoryPicker value={workingDirectory} onChange={onWorkingDirectoryChange} disabled={sending} />}
        <Tooltip title="添加参考文件（文本、代码、Markdown，单个不超过 128 KB）">
          <Button type="text" aria-label="添加参考文件" icon={<Paperclip size={20} />} disabled={sending || reading}
            loading={reading} onClick={() => fileInput.current?.click()} />
        </Tooltip>
        <Button type="primary" shape="circle" htmlType="submit" aria-label={sending ? '处理中' : '发送'}
          disabled={disabled || reading || !input.trim()} loading={sending} icon={<ArrowUp size={21} />} />
      </Flex>
    </div>
    <input ref={fileInput} type="file" multiple hidden aria-label="参考文件"
      onChange={event => void addFiles(Array.from(event.target.files ?? []))} />
    {error && <Alert type="error" title={error} showIcon closable onClose={() => setError('')} />}
  </form>
}
