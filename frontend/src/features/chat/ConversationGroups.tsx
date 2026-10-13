import { useEffect, useState } from 'react'
import { Button } from 'antd'
import { ChevronDown, ChevronRight, Folder, Plus } from 'lucide-react'
import { api, type ConversationSummary } from '@/api/client'
import { useStoredBoolean } from '@/hooks/useStoredBoolean'
import { ConversationHistoryItem } from './ConversationHistoryItem'
import { DirectoryPicker } from './DirectoryPicker'

type Props = {
  history: ConversationSummary[]
  conversationId: string
  onNavigate: () => void
  onNew: (directory?: string) => void
  onRename: (id: string, title: string) => Promise<void>
  onDelete: (id: string) => Promise<void>
}

function ConversationGroup(props: Props & { directory: string; title: string }) {
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
  }, [props.history, directory, collapsed])
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
        icon={<Plus size={15} />} onClick={() => props.onNew(directory || undefined)} />}
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
  const [directories, setDirectories] = useState<string[]>([])
  const [error, setError] = useState('')
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
      setDirectories(current => [...new Set([...current, result.path])].sort())
      setError('')
      props.onNew(result.path)
    } catch (reason) { setError(reason instanceof Error ? reason.message : '添加工作区失败') }
  }
  return <>
    <ConversationGroup {...props} directory="" title="对话" />
    <div className="conversation-group-heading workspace-heading"><span>工作区</span>
      <DirectoryPicker label="添加工作区" disabled={false} onChange={directory => void add(directory)} />
    </div>
    {error && <span role="alert" className="hint">{error}</span>}
    {directories.map(directory => <ConversationGroup key={directory} {...props} directory={directory}
      title={directory.split('/').filter(Boolean).pop() || directory} />)}
    {!directories.length && <span className="hint group-empty">添加目录，开始工作区对话</span>}
  </>
}
