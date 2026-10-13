import { useState } from 'react'
import { Alert, Button, Dropdown, Input, Modal } from 'antd'
import { MoreHorizontal, Pencil, Trash2, Workflow } from 'lucide-react'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '@/api/client'

export function ConversationHistoryItem({ id, title, active, onNavigate, onRename, onDelete }: {
  id: string; title: string; active: boolean; onNavigate: () => void
  onRename: (id: string, title: string) => Promise<void>
  onDelete: (id: string) => Promise<void>
}) {
  const navigate = useNavigate()
  const [extracting, setExtracting] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)
  const [action, setAction] = useState<'rename' | 'delete' | null>(null)
  const [draft, setDraft] = useState(title)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  async function submit() {
    if (busy || (action === 'rename' && !draft.trim())) return
    setBusy(true)
    setError('')
    try {
      if (action === 'rename') await onRename(id, draft.trim())
      else await onDelete(id)
      setAction(null)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '操作失败，请重试')
    } finally { setBusy(false) }
  }
  async function extract() {
    if (extracting) return
    setExtracting(true)
    setError('')
    try {
      const result = await api.extractConversationWorkflows(id)
      onNavigate()
      navigate(`/chat/${result.conversationId}`)
    } catch (reason) { setError(reason instanceof Error ? reason.message : '提取失败，请重试') }
    finally { setExtracting(false) }
  }
  const menu = { items: [
    { key: 'extract', label: extracting ? '正在创建提取对话…' : '提取工作流', icon: <Workflow size={14} />, disabled: extracting },
    { key: 'rename', label: '重命名', icon: <Pencil size={14} /> },
    { key: 'delete', label: '删除', icon: <Trash2 size={14} /> },
  ], onClick: ({ key }: { key: string }) => {
    setMenuOpen(false)
    if (key === 'extract') { void extract(); return }
    setDraft(title); setError(''); setAction(key as 'rename' | 'delete')
  } }
  return <>
    <Dropdown trigger={['contextMenu']} menu={menu}>
    <div className={`history-row ${active ? 'active' : ''}`}>
      <Link className="history-item" to={`/chat/${id}`} title={title}
        aria-current={active ? 'page' : undefined} onClick={onNavigate}>
        <strong>{title}</strong>
      </Link>
      <Dropdown open={menuOpen} onOpenChange={setMenuOpen} trigger={['click']} menu={menu}>
        <Button type="text" className="history-menu" aria-expanded={menuOpen} aria-label={`管理对话：${title}`} icon={<MoreHorizontal size={18} />} />
      </Dropdown>
    </div>
    </Dropdown>
    {error && !action && <Alert type="error" showIcon title={error} closable onClose={() => setError('')} />}
    <Modal title={action === 'rename' ? '重命名对话' : '删除对话'} open={action !== null}
      onCancel={() => { if (!busy) setAction(null) }} onOk={() => void submit()}
      okText={action === 'rename' ? '保存' : '删除'} cancelText="取消" confirmLoading={busy}
      cancelButtonProps={{ disabled: busy }} okButtonProps={{ disabled: action === 'rename' && !draft.trim() }}>
      {action === 'rename' ? <Input aria-label="对话标题" value={draft} maxLength={200}
        disabled={busy} onChange={event => setDraft(event.target.value)} onPressEnter={() => void submit()} />
        : <p>删除“{title}”？该对话将从历史记录中移除，正在进行的任务会被取消。</p>}
      {error && <Alert type="error" showIcon title={error} style={{ marginTop: 12 }} />}
    </Modal>
  </>
}
