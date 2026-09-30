import { useState } from 'react'
import { Alert, Button, Dropdown, Input, Modal } from 'antd'
import { MoreHorizontal, Pencil, Trash2 } from 'lucide-react'
import { Link } from 'react-router-dom'

export function ConversationHistoryItem({ id, title, active, onNavigate, onRename, onDelete }: {
  id: string; title: string; active: boolean; onNavigate: () => void
  onRename: (id: string, title: string) => Promise<void>
  onDelete: (id: string) => Promise<void>
}) {
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
  return <>
    <div className={`history-row ${active ? 'active' : ''}`}>
      <Link className="history-item" to={`/chat/${id}`} title={title}
        aria-current={active ? 'page' : undefined} onClick={onNavigate}>
        <strong>{title}</strong>
      </Link>
      <Dropdown trigger={['click']} menu={{ items: [
        { key: 'rename', label: '重命名', icon: <Pencil size={14} /> },
        { key: 'delete', label: '删除', icon: <Trash2 size={14} /> },
      ], onClick: ({ key }) => { setDraft(title); setError(''); setAction(key as 'rename' | 'delete') } }}>
        <Button type="text" className="history-menu" aria-label={`管理对话：${title}`} icon={<MoreHorizontal size={18} />} />
      </Dropdown>
    </div>
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
