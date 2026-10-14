import { useEffect, useState } from 'react'
import { Alert, Button, Dropdown, Input, Modal, Radio } from 'antd'
import { ChevronDown, ChevronRight, Folder, MoreHorizontal, Pencil, Plus, Trash2, FolderOpen } from 'lucide-react'
import { api, type ChatWorkspaceSummary, type ConversationSummary } from '@/api/client'
import { useStoredBoolean } from '@/hooks/useStoredBoolean'
import { ConversationHistoryItem } from './ConversationHistoryItem'
import { DirectoryPicker } from './DirectoryPicker'

type Props = {
  history: ConversationSummary[]
  conversationId: string
  onNavigate: () => void
  onNew: (directory?: string, workspacePath?: string) => void
  onRename: (id: string, title: string) => Promise<void>
  onDelete: (id: string) => Promise<void>
  onWorkspaceChanged?: (path: string, nextPath?: string, deletedIds?: string[]) => Promise<void>
  revision?: number
}

function ConversationGroup(props: Props & { directory: string; title: string; onManage?: (action: string) => void }) {
  const { directory, title } = props
  const [collapsed, setCollapsed] = useStoredBoolean(`chat-group-collapsed:${directory}`)
  const [items, setItems] = useState<ConversationSummary[]>([])
  const [cursor, setCursor] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => {
    if (collapsed) return
    let disposed = false
    setLoading(true)
    setError('')
    api.listConversations(undefined, undefined, directory).then(page => {
      if (!disposed) { setItems(page.items); setCursor(page.nextCursor) }
    }).catch(() => { if (!disposed) setError('读取对话失败') })
      .finally(() => { if (!disposed) setLoading(false) })
    return () => { disposed = true }
  }, [props.history, directory, collapsed, props.revision])
  async function loadMore() {
    if (!cursor || loading) return
    setLoading(true)
    try {
      const page = await api.listConversations(cursor, undefined, directory)
      setItems(current => [...current, ...page.items.filter(item => !current.some(old => old.id === item.id))])
      setCursor(page.nextCursor)
      setError('')
    } catch { setError('读取对话失败') } finally { setLoading(false) }
  }
  return <section className={`conversation-group${directory ? ' workspace-group' : ''}`} aria-label={title}>
    <div className="conversation-group-heading">
      <button className="conversation-group-toggle" title={directory || title}
        aria-expanded={!collapsed} onClick={() => setCollapsed(!collapsed)}>
        {collapsed ? <ChevronRight size={14} /> : <ChevronDown size={14} />}
        {directory && <Folder size={15} />}<span>{title}</span>
      </button>
      {!collapsed && <Button type="text" aria-label={`在${title}中新建对话`} title="新建对话"
        icon={<Plus size={15} />} onClick={() => props.onNew(directory || undefined, directory || undefined)} />}
      {directory && <Dropdown trigger={['click']} menu={{ items: [
        { key: 'rename', label: '重命名', icon: <Pencil size={14} /> },
        { key: 'directory', label: '修改目录', icon: <FolderOpen size={14} /> },
        { key: 'delete', label: '删除工作区', icon: <Trash2 size={14} />, danger: true },
      ], onClick: ({ key }) => props.onManage?.(key) }}>
        <Button type="text" className="workspace-menu" aria-label={`管理工作区：${title}`} icon={<MoreHorizontal size={15} />} />
      </Dropdown>}
    </div>
    {!collapsed && <nav aria-label={`${title}的对话`}>
      {items.map(item => <ConversationHistoryItem key={item.id} id={item.id} title={item.title}
        active={item.id === props.conversationId} onNavigate={props.onNavigate}
        onRename={props.onRename} onDelete={props.onDelete} />)}
      {!loading && !error && !items.length && <span className="hint group-empty">暂无对话</span>}
      {error && <span role="alert" className="hint">{error}</span>}
      {cursor && <Button type="text" loading={loading} onClick={() => void loadMore()}>加载更多</Button>}
    </nav>}
  </section>
}

export function ConversationGroups(props: Props) {
  const [directories, setDirectories] = useState<ChatWorkspaceSummary[]>([])
  const [error, setError] = useState('')
  const [revision, setRevision] = useState(0)
  const [editing, setEditing] = useState<{ workspace: ChatWorkspaceSummary; action: string }>()
  const [draft, setDraft] = useState('')
  const [removeChats, setRemoveChats] = useState(false)
  const [busy, setBusy] = useState(false)
  const [editError, setEditError] = useState('')
  useEffect(() => {
    let disposed = false
    api.listChatWorkspaces().then(result => { if (!disposed) setDirectories(result.items) })
      .catch(() => { if (!disposed) setError('读取工作区失败') })
    return () => { disposed = true }
  }, [props.history])
  async function add(directory?: string) {
    if (!directory) return
    try {
      const result = await api.addChatWorkspace(directory)
      setDirectories(current => [...current.filter(item => item.path !== result.path), result].sort((a, b) => a.path.localeCompare(b.path)))
      setError('')
      props.onNew(result.path, result.path)
    } catch (reason) { setError(reason instanceof Error ? reason.message : '添加工作区失败') }
  }
  async function save() {
    if (!editing || busy) return
    setBusy(true); setEditError('')
    const { workspace, action } = editing
    try {
      let nextPath: string | undefined
      let deletedIds: string[] = []
      if (action === 'delete') {
        const result = await api.deleteChatWorkspace(workspace.path, removeChats)
        setDirectories(current => current.filter(item => item.path !== workspace.path))
        if (removeChats) deletedIds = result.conversationIds
      } else {
        const result = await api.updateChatWorkspace(workspace.path, action === 'rename' ? { name: draft.trim() } : { workingDirectory: draft.trim() })
        nextPath = result.path
        setDirectories(current => current.map(item => item.path === workspace.path ? result : item))
      }
      setRevision(current => current + 1)
      setEditing(undefined)
      try { await props.onWorkspaceChanged?.(workspace.path, nextPath, deletedIds) }
      catch { setError("工作区已保存，对话刷新失败，请刷新页面") }
    } catch (reason) { setEditError(reason instanceof Error ? reason.message : '保存失败，请重试') }
    finally { setBusy(false) }
  }
  return <>
    <ConversationGroup {...props} revision={revision} directory="" title="对话" />
    <div className="conversation-group-heading workspace-heading"><span>工作区</span>
      <DirectoryPicker label="添加工作区" disabled={false} onChange={directory => void add(directory)} />
    </div>
    {error && <span role="alert" className="hint">{error}</span>}
    {directories.map(workspace => <ConversationGroup key={workspace.path} {...props} revision={revision} directory={workspace.path}
      title={workspace.name} onManage={action => {
        setEditing({ workspace, action }); setDraft(action === 'rename' ? workspace.name : workspace.path)
        setRemoveChats(false); setEditError('')
      }} />)}
    <Modal title={editing?.action === 'rename' ? '重命名工作区' : editing?.action === 'directory' ? '修改工作区目录' : '删除工作区'}
      open={!!editing} onCancel={() => { if (!busy) setEditing(undefined) }} onOk={() => void save()}
      confirmLoading={busy} cancelButtonProps={{ disabled: busy }} cancelText="取消"
      okText={editing?.action === 'delete' ? '删除工作区' : '保存'}
      okButtonProps={{ danger: editing?.action === 'delete', disabled: editing?.action !== 'delete' && !draft.trim() }}>
      {editing?.action === 'delete' ? <>
        <p>删除“{editing.workspace.name}”后，其中的对话如何处理？磁盘目录和文件不会删除。</p>
        <Radio.Group value={removeChats} onChange={event => setRemoveChats(event.target.value)} disabled={busy}>
          <Radio value={false}>移到普通对话</Radio><Radio value={true}>一起删除对话</Radio>
        </Radio.Group>
        {removeChats && <p className="hint">正在进行的任务也会被取消。</p>}
      </> : <>
        <div className="workspace-edit-field">
          <Input aria-label={editing?.action === 'rename' ? '工作区名称' : '工作区目录'} value={draft}
            maxLength={editing?.action === 'rename' ? 200 : 4096} disabled={busy} onChange={event => setDraft(event.target.value)} />
          {editing?.action === 'directory' && <DirectoryPicker value={draft} disabled={busy} onChange={path => { if (path) setDraft(path) }} />}
        </div>
        {editing?.action === 'directory' && <p className="hint">工作区内使用原目录的对话将改用新目录，已开始的任务仍使用原目录。</p>}
      </>}
      {editError && <Alert type="error" title={editError} />}
    </Modal>
    {!directories.length && <span className="hint group-empty">添加目录，开始工作区对话</span>}
  </>
}
