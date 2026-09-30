import { Alert, Empty, Input, Table, Tag } from 'antd'
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { capabilityDescription } from '@/lib/capabilityPresentation'
import { request } from '@/api/http'

type Capability = {id: string; capabilityId: string; name: string; displayName?: string; description?: string; version: string; interfaces: Record<string, {description?: string}>}

export default function CapabilitiesPage() {
  const [capabilities, setCapabilities] = useState<Capability[]>([])
  const [query, setQuery] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  useEffect(() => {
    request<Capability[]>('/capability-catalog').then(setCapabilities).catch((reason: Error) => setError(reason.message)).finally(() => setLoading(false))
  }, [])
  return <div className="page">
    <header className="page-header"><div><span className="eyebrow">工作空间 / SkillX</span><h1>SkillX</h1><p>查看 Agent 与工作流可使用的工具、组件和连接。</p></div></header>
    {error && <Alert type="error" title={error} showIcon />}
    <div className="collection-toolbar"><h2>SkillX 版本</h2><Input.Search aria-label="搜索能力" placeholder="搜索能力名称或用途" allowClear value={query} onChange={event => setQuery(event.target.value)} style={{ maxWidth: 300 }} /></div>
    <Table<Capability> rowKey="id" loading={loading} scroll={{ x: 640 }}
      dataSource={capabilities.filter(item => `${item.name} ${item.displayName ?? ""} ${capabilityDescription(item)}`.toLowerCase().includes(query.toLowerCase()))}
      pagination={{ pageSize: 10, hideOnSinglePage: true }}
      locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无匹配的能力" /> }}
      columns={[
        { title: '名称', dataIndex: 'name', render: (name, item) => <div><Link to={`/capabilities/${item.capabilityId}`}><strong>{item.displayName || name}</strong></Link><div className="table-secondary">{capabilityDescription(item)}</div></div> },
        { title: '接口', width: 140, render: (_, item) => Object.keys(item.interfaces).map(name => <Tag key={name}>{name}</Tag>) },
        { title: '当前版本', width: 110, dataIndex: 'version', render: value => <code>{value}</code> },
      ]} />
  </div>
}
