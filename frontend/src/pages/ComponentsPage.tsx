import { ArrowUpRight, Boxes, Gauge, ShieldCheck, Sparkles } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { api, type ComponentSummary } from '@/api/client'

export default function ComponentsPage() {
  const [components, setComponents] = useState<ComponentSummary[]>([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    api
      .listComponents()
      .then(setComponents)
      .catch((reason: Error) => setError(reason.message))
      .finally(() => setLoading(false))
  }, [])

  return (
    <div className="page">
      <header className="page-header">
        <div>
          <span className="eyebrow">COMPONENT REGISTRY</span>
          <h1>已发布组件</h1>
          <p>以锁定版本运行确定性工作流，模型仅在显式节点介入。</p>
        </div>
        <div className="header-stat">
          <Boxes size={18} />
          <span>{components.length}</span>
          <small>可运行组件</small>
        </div>
      </header>

      <div className="filter-strip">
        <button className="filter-chip active">全部</button>
        <button className="filter-chip">零 LLM</button>
        <button className="filter-chip">局部 LLM</button>
        <button className="filter-chip">Agent 工作流</button>
      </div>

      {loading && <div className="skeleton-grid" aria-label="加载组件" />}
      {error && <div className="error-panel">{error}</div>}
      {!loading && !error && components.length === 0 && (
        <section className="empty-state">
          <Boxes size={30} />
          <h2>尚无已发布组件</h2>
          <p>先在工作流页面创建定义、通过静态校验并发布版本，再绑定为组件。</p>
          <Link className="button primary" to="/workflows">
            打开工作流
          </Link>
        </section>
      )}
      <section className="component-grid">
        {components.map((component, index) => (
          <Link
            className="component-card"
            to={`/components/${component.id}`}
            key={component.id}
            style={{ animationDelay: `${index * 70}ms` }}
          >
            <div className="card-topline">
              <span className="component-icon">
                <Sparkles size={17} />
              </span>
              <span className="mode-tag">DIRECT READY</span>
              <ArrowUpRight size={17} />
            </div>
            <h2>{component.name}</h2>
            <p>{component.description || '该组件暂无补充说明。'}</p>
            <div className="card-metrics">
              <span>
                <Gauge size={14} /> 锁定版本
              </span>
              <span>
                <ShieldCheck size={14} /> {component.visibility}
              </span>
            </div>
          </Link>
        ))}
      </section>
    </div>
  )
}
