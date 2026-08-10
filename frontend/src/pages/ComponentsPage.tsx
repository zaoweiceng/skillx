import { ArrowUpRight, Boxes, Gauge, ShieldCheck, Sparkles } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { api, type ComponentSummary } from '@/api/client'

export default function ComponentsPage() {
  const [components, setComponents] = useState<ComponentSummary[]>([])
  const [filter, setFilter] = useState<'all' | 'zero_llm' | 'hybrid' | 'agent'>('all')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    api
      .listComponents()
      .then(setComponents)
      .catch((reason: Error) => setError(reason.message))
      .finally(() => setLoading(false))
  }, [])
  const filtered =
    filter === 'all'
      ? components
      : components.filter((component) => component.executionClass === filter)
  const filters = [
    ['all', '全部'],
    ['zero_llm', '零 LLM'],
    ['hybrid', '局部 LLM'],
    ['agent', 'Agent 工作流'],
  ] as const

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
        {filters.map(([value, label]) => (
          <button
            className={`filter-chip ${filter === value ? 'active' : ''}`}
            type="button"
            aria-pressed={filter === value}
            onClick={() => setFilter(value)}
            key={value}
          >
            {label}
          </button>
        ))}
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
      {!loading && !error && components.length > 0 && filtered.length === 0 && (
        <section className="empty-state">
          <Boxes size={30} />
          <h2>当前分类暂无组件</h2>
          <p>切换其他执行分类查看已发布组件。</p>
        </section>
      )}
      <section className="component-grid">
        {filtered.map((component, index) => (
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
              <span className="mode-tag">
                {component.executionClass === 'zero_llm'
                  ? 'ZERO LLM'
                  : component.executionClass.toUpperCase()}
              </span>
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
