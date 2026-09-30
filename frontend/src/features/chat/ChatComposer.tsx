import { Alert, Button, Flex, Input, Tag, Tooltip } from 'antd'
import { ArrowUp, FileText, FolderOpen, Paperclip } from 'lucide-react'
import { useRef, useState, type FormEvent, type ReactNode } from 'react'
import type { ChatImageInput, ChatReferenceFile } from '@/api/client'
import styles from './chat.module.css'
import { DirectoryPicker } from './DirectoryPicker'
import { ChatImageAttachments } from './ChatImageAttachments'

const MAX_FILES = 8
const MAX_FILE_BYTES = 128 * 1024
const MAX_TOTAL_BYTES = 256 * 1024
const MAX_IMAGE_BYTES = 5 * 1024 * 1024
const MAX_IMAGES_BYTES = 20 * 1024 * 1024
const IMAGE_TYPES = new Set(['image/png', 'image/jpeg', 'image/webp', 'image/gif'])
type DraftImage = ChatImageInput & { size: number }

function readImage(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result).split(',')[1])
    reader.onerror = () => reject(new Error(`${file.name} 图片读取失败`))
    reader.readAsDataURL(file)
  })
}

export function ChatComposer({ input, onInputChange, disabled, sending, controls, onSend, directoryControl, workingDirectory, onWorkingDirectoryChange }: {
  workingDirectory?: string
  onWorkingDirectoryChange: (path: string | undefined) => void
  input: string
  onInputChange: (value: string) => void
  disabled: boolean
  sending: boolean
  controls: ReactNode
  directoryControl?: ReactNode
  onSend: (event: FormEvent, files: ChatReferenceFile[], workingDirectory?: string, images?: ChatImageInput[]) => Promise<boolean>
}) {
  const fileInput = useRef<HTMLInputElement>(null)
  const [files, setFiles] = useState<ChatReferenceFile[]>([])
  const [images, setImages] = useState<DraftImage[]>([])
  const filesRef = useRef(files)
  const imagesRef = useRef(images)
  const queue = useRef(Promise.resolve())
  const pendingReads = useRef(0)
  const [error, setError] = useState('')
  const [reading, setReading] = useState(false)

  function addFiles(selected: File[]) {
    if (!selected.length || sending) return
    pendingReads.current++
    setError('')
    setReading(true)
    queue.current = queue.current.then(() => readFiles(selected))
  }

  async function readFiles(selected: File[]) {
    try {
      const selectedImages = selected.filter(file => file.type.startsWith('image/'))
      const selectedTexts = selected.filter(file => !file.type.startsWith('image/'))
      if (filesRef.current.length + selectedTexts.length > MAX_FILES) throw new Error(`最多选择 ${MAX_FILES} 个参考文件`)
      if (imagesRef.current.length + selectedImages.length > MAX_FILES) throw new Error(`最多添加 ${MAX_FILES} 张图片`)
      const incomingImages = await Promise.all(selectedImages.map(async file => {
        if (!IMAGE_TYPES.has(file.type)) throw new Error('图片仅支持 PNG、JPEG、WebP 和 GIF')
        if (file.size > MAX_IMAGE_BYTES) throw new Error(`${file.name} 超过 5 MB，请选择较小的图片`)
        return { name: file.name, mimeType: file.type, data: await readImage(file), size: file.size }
      }))
      const nextImages = [...imagesRef.current, ...incomingImages]
      if (nextImages.reduce((sum, image) => sum + image.size, 0) > MAX_IMAGES_BYTES) throw new Error('图片总大小不能超过 20 MB')
      const incoming = await Promise.all(selectedTexts.map(async file => {
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
      const next = [...filesRef.current, ...incoming]
      if (new TextEncoder().encode(next.map(file => file.content).join('')).length > MAX_TOTAL_BYTES) {
        throw new Error('参考文件总大小不能超过 256 KB')
      }
      filesRef.current = next
      imagesRef.current = nextImages
      setFiles(next)
      setImages(nextImages)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '文件读取失败')
    } finally {
      pendingReads.current--
      setReading(pendingReads.current > 0)
      if (fileInput.current) fileInput.current.value = ''
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (disabled || reading || (!input.trim() && !images.length)) return
    setError('')
    const sent = images.length
      ? await onSend(event, files, workingDirectory, images.map(({ name, mimeType, data }) => ({ name, mimeType, data })))
      : await onSend(event, files, workingDirectory)
    if (sent) { filesRef.current = []; imagesRef.current = []; setFiles([]); setImages([]) }
  }

  return <form className={styles.composer} onSubmit={submit} onPaste={event => {
    const pasted = Array.from(event.clipboardData.items ?? [])
      .filter(item => item.kind === 'file' && item.type.startsWith('image/'))
      .map(item => item.getAsFile()).filter((file): file is File => !!file)
    if (pasted.length) { event.preventDefault(); addFiles(pasted) }
  }}>
    <ChatImageAttachments images={images.map(image => ({ name: image.name, url: `data:${image.mimeType};base64,${image.data}` }))}
      disabled={sending || reading} onRemove={index => { imagesRef.current = imagesRef.current.filter((_, i) => i !== index); setImages(imagesRef.current) }} />
    {(workingDirectory || files.length > 0) && <Flex gap={6} wrap className={styles.attachments}>
      {workingDirectory && <Tag className={styles.directoryTag} icon={<FolderOpen size={13} />} closable={!sending}
      onClose={event => { event.preventDefault(); onWorkingDirectoryChange(undefined) }} title={workingDirectory}>
        <span className={styles.directoryPath}>{workingDirectory}</span>
      </Tag>}
      {files.map((file, index) => <Tag key={`${file.name}-${index}`} icon={<FileText size={13} />}
        closable={!sending && !reading} onClose={() => { filesRef.current = filesRef.current.filter((_, i) => i !== index); setFiles(filesRef.current) }}>
        {file.name}
      </Tag>)}
    </Flex>}
    <Input.TextArea variant="borderless" aria-label="对话输入" placeholder="描述一个需要探索或固化的任务..."
      value={input} onChange={event => onInputChange(event.target.value)} autoSize={{minRows: 2, maxRows: 8}} />
    <div className={styles.composerToolbar}>
      <div className={styles.composerOptions}>{controls}</div>
      <Flex gap={8} align="center" className={styles.composerActions}>
        {directoryControl ?? <DirectoryPicker value={workingDirectory} onChange={onWorkingDirectoryChange} disabled={sending} />}
        <Tooltip title="添加图片或参考文件（图片单张 ≤ 5 MB，文本单个 ≤ 128 KB）；也可直接粘贴图片">
          <Button type="text" aria-label="添加参考文件" icon={<Paperclip size={20} />} disabled={sending || reading}
            loading={reading} onClick={() => fileInput.current?.click()} />
        </Tooltip>
        <Button type="primary" shape="circle" htmlType="submit" aria-label={sending ? '处理中' : '发送'}
          disabled={disabled || reading || (!input.trim() && !images.length)} loading={sending} icon={<ArrowUp size={21} />} />
      </Flex>
    </div>
    <input ref={fileInput} type="file" multiple hidden aria-label="参考文件"
      onChange={event => void addFiles(Array.from(event.target.files ?? []))} />
    {error && <Alert type="error" title={error} showIcon closable onClose={() => setError('')} />}
  </form>
}
