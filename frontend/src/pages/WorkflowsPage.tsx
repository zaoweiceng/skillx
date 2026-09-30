import { Alert, Button, Empty, Input, Skeleton, Tag } from 'antd'
import { ArrowUpRight, FileCode2, Pencil, Plus, Sparkles } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import { api, type ComponentSummary, type Workflow } from '@/api/client'
import { request } from '@/api/http'
import { workflowDescription } from '@/lib/capabilityPresentation'
import { defaultWorkflow } from '@/lib/defaultWorkflow'

export default function WorkflowsPage() {
  const navigate = useNavigate()
  const [workflows, setWorkflows] = useState<Workflow[]>([])
  const [components, setComponents] = useState<ComponentSummary[]>([])
  const [publishedIds, setPublishedIds] = useState<string[]>([])
  const [error, setError] = useState('')
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState('all')
  const [loading, setLoading] = useState(true)
  const [creating, setCreating] = useState(false)

  useEffect(() => {
    Promise.all([api.listWorkflows(), api.listComponents(), request<Array<{capabilityId: string}>>('/capability-catalog')])
      .then(([drafts, published, catalog]) => {
        setWorkflows(drafts)
        setComponents(published)
        setPublishedIds(catalog.map(item => item.capabilityId))
      })
      .catch((reason: Error) => setError(reason.message))
      .finally(() => setLoading(false))
  }, [])

  async function createWorkflow() {
    setCreating(true)
    try {
      const workflow = await api.createWorkflow('问候工作流', defaultWorkflow)
      navigate(`/workflows/${workflow.id}/edit`)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '创建失败')
    } finally { setCreating(false) }
  }

  const items = workflows.map(workflow => {
    const component = components.find(item => item.workflowId === workflow.id)
    const published = Boolean(component) || publishedIds.includes(workflow.id)
    const description = component?.capabilityManifest?.summary || workflowDescription(workflow.definition.metadata)
    return {workflow, component, published, description}
  })
  const filtered = items.filter(({workflow, published, description}) =>
    (filter === 'all' || (filter === 'draft' ? !published : published)) &&
    `${workflow.name} ${description}`.toLowerCase().includes(query.toLowerCase()))

  return <div className="page">
    <header className="page-header">
      <div><span className="eyebrow">工作空间 / 工作流</span><h1>工作流</h1><p>管理和复用工作流，按需编辑、发布与运行。</p></div>
      <Button type="primary" icon={<Plus size={15} />} loading={creating} onClick={createWorkflow}>新建工作流</Button>
    </header>
    <div className="collection-toolbar">
      <Input.Search aria-label="搜索工作流" placeholder="搜索工作流名称或用途" allowClear value={query} onChange={event => setQuery(event.target.value)} style={{maxWidth: 300}} />
      <div className="filter-strip">
        {([['all', '全部'], ['published', '已发布'], ['draft', '草稿']] as const).map(([value, label]) =>
          <Button key={value} className={`filter-chip ${filter === value ? 'active' : ''}`} aria-pressed={filter === value} onClick={() => setFilter(value)}>{label}</Button>)}
      </div>
    </div>
    {loading && <Skeleton active paragraph={{rows: 4}} />}
    {error && <Alert type="error" title={error} showIcon />}
    {!loading && !error && filtered.length === 0 && <Empty description={items.length ? '没有匹配的工作流' : '暂无工作流'} />}
    <section className="component-grid">
      {filtered.map(({workflow, component, published, description}) => {
        const href = component ? `/workflows/published/${component.id}` : `/workflows/${workflow.id}/${published ? 'run' : 'edit'}`
        return <article className="component-card" key={workflow.id}>
          <Link to={href} className="workflow-card-main">
            <div className="card-topline"><span className="component-icon">{published ? <Sparkles size={17} /> : <FileCode2 size={17} />}</span><Tag>{published ? '已发布' : '草稿'}</Tag><ArrowUpRight size={17} /></div>
            <h2>{workflow.name}</h2><p>{description}</p>
          </Link>
          <div className="card-metrics">
            <span>{component ? ({zero_llm: '固定流程', hybrid: '混合执行', agent: 'Agent 工作流'}[component.executionClass]) : published ? '可运行' : '待发布'}</span>
            <Link to={`/workflows/${workflow.id}/edit`}><Pencil size={13} /> 编辑</Link>
          </div>
        </article>
      })}
    </section>
  </div>
}
