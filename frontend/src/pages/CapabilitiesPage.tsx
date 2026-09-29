import { Alert, Card, Empty, Input, Table, Tag } from 'antd'
import { Blocks, Cable, LockKeyhole } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { request } from '@/api/http'

type Capability = {id: string; capabilityId: string; name: string; version: string; interfaces: Record<string, {description?: string}>}

export default function CapabilitiesPage() {
  const [capabilities, setCapabilities] = useState<Capability[]>([])
  const [query, setQuery] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  useEffect(() => {
    request<Capability[]>('/capability-catalog').then(setCapabilities).catch((reason: Error) => setError(reason.message)).finally(() => setLoading(false))
  }, [])
  return <div className="page">
    <header className="page-header"><div><span className="eyebrow">工作空间 / 能力</span><h1>能力清单</h1><p>查看 Agent 与工作流可使用的工具、组件和连接。</p></div></header>
    {error && <Alert type="error" title={error} showIcon />}
    <section className="capability-overview">
      <Card size="small"><Blocks size={20} /><strong>{capabilities.length}</strong><span>已注册能力</span></Card>
      <Card size="small"><LockKeyhole size={20} /><strong>按需授权</strong><span>仅允许已授予的工具</span></Card>
      <Card size="small"><Cable size={20} /><strong>版本锁定</strong><span>执行已发布的能力版本</span></Card>
    </section>
    <div className="collection-toolbar"><h2>能力版本</h2><Input.Search aria-label="搜索能力" placeholder="搜索能力名称或用途" allowClear value={query} onChange={event => setQuery(event.target.value)} style={{ maxWidth: 300 }} /></div>
    <Table<Capability> rowKey="id" loading={loading} scroll={{ x: 640 }}
      dataSource={capabilities.filter(item => `${item.name} ${Object.values(item.interfaces).map(i => i.description).join(" ")}`.toLowerCase().includes(query.toLowerCase()))}
      pagination={{ pageSize: 10, hideOnSinglePage: true }}
      locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无匹配的能力" /> }}
      columns={[
        { title: '名称', dataIndex: 'name', render: (name, item) => <div><Link to={`/capabilities/${item.capabilityId}`}><strong>{name}</strong></Link><div className="table-secondary">{item.id}</div></div> },
        { title: '接口', render: (_, item) => Object.keys(item.interfaces).map(name => <Tag key={name}>{name}</Tag>) },
        { title: '当前版本', dataIndex: 'version', render: value => <code>{value}</code> },
      ]} />
  </div>
}
