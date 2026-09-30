import { Alert, Button, Empty, Flex, Input, Modal, Spin, Tooltip, Typography } from 'antd'
import { ArrowUp, FolderOpen } from 'lucide-react'
import { useState } from 'react'
import { api } from '@/api/client'

export function DirectoryPicker({ value, onChange, disabled }: {
  value?: string; onChange: (path: string | undefined) => void; disabled: boolean
}) {
  const [open, setOpen] = useState(false)
  const [path, setPath] = useState('')
  const [listing, setListing] = useState<Awaited<ReturnType<typeof api.workspaceDirectories>>>()
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  async function browse(target?: string) {
    setLoading(true)
    setError('')
    try {
      const result = await api.workspaceDirectories(target)
      setListing(result)
      setPath(result.path)
    } catch (reason) {
      setListing(undefined)
      setError(reason instanceof Error ? reason.message : '无法读取目录')
    } finally { setLoading(false) }
  }
  return <>
    <Tooltip title={value ? `工作目录：${value}` : '选择工作目录'}>
      <Button type="text" aria-label="选择工作目录" icon={<FolderOpen size={19} />} disabled={disabled}
        onClick={() => { setOpen(true); void browse(value) }} />
    </Tooltip>
    <Modal title="选择工作目录" open={open} onCancel={() => setOpen(false)}
      okText="使用此目录" cancelText="取消" okButtonProps={{ disabled: loading || !listing || path !== listing.path }}
      onOk={() => { if (listing) onChange(listing.path); setOpen(false) }}>
      <Typography.Paragraph type="secondary">选择运行服务所在电脑上的目录，任务中的代码将在该目录执行。依赖与执行记录仍保存在任务环境中。</Typography.Paragraph>
      <Flex gap={8}>
        <Button aria-label="上一级目录" icon={<ArrowUp size={16} />} disabled={loading || !listing || listing.parent === listing.path}
          onClick={() => void browse(listing?.parent)} />
        <Input.Search aria-label="目录路径" value={path} onChange={event => setPath(event.target.value)}
          onSearch={target => void browse(target)} enterButton="打开" loading={loading} />
      </Flex>
      {error && <Alert type="error" title={error} style={{ marginTop: 12 }} />}
      <Spin spinning={loading}>
        <div style={{ height: 280, overflowY: 'auto', marginTop: 12 }}>
          {listing?.directories.map(directory => <Button key={directory.path} type="text" block
            style={{ textAlign: 'left', justifyContent: 'flex-start' }} icon={<FolderOpen size={16} />}
            onClick={() => void browse(directory.path)}>{directory.name}</Button>)}
          {listing && !listing.directories.length && <Empty description="没有子目录，可直接选择当前目录" image={Empty.PRESENTED_IMAGE_SIMPLE} />}
        </div>
      </Spin>
    </Modal>
  </>
}
