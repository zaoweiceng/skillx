import Form from '@rjsf/core'
import validator from '@rjsf/validator-ajv8'
import { ArrowLeft, Clock3, Play, ShieldAlert } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'

import { api, type ComponentSummary } from '@/api/client'
import { WorkflowGraph } from '@/components/WorkflowGraph'

export default function ComponentDetailPage() {
  const { id } = useParams()
  const navigate = useNavigate()
  const [component, setComponent] = useState<ComponentSummary>()
  const [formData, setFormData] = useState<Record<string, unknown>>({})
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)

  useEffect(() => {
    if (!id) return
    api.getComponent(id).then(setComponent).catch((reason: Error) => setError(reason.message))
  }, [id])

  async function runComponent() {
    if (!id) return
    setSubmitting(true)
    setError('')
    try {
      const run = await api.createRun(id, formData)
      navigate(`/runs/${run.id}`)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '创建运行失败')
    } finally {
      setSubmitting(false)
    }
  }

  if (error && !component) return <div className="page error-panel">{error}</div>
  if (!component?.definition) return <div className="page loading-panel">正在加载组件契约...</div>

  return (
    <div className="page detail-page">
      <Link className="back-link" to="/components">
        <ArrowLeft size={15} /> 返回组件
      </Link>
      <header className="page-header compact-header">
        <div>
          <span className="eyebrow">LOCKED WORKFLOW COMPONENT</span>
          <h1>{component.name}</h1>
          <p>{component.description || '通过结构化输入直接启动锁定的工作流版本。'}</p>
        </div>
        <span className="mode-tag large">
          {
            {
              zero_llm: '零 LLM',
              hybrid: '局部 LLM',
              agent: 'Agent 工作流',
            }[component.executionClass]
          }
        </span>
      </header>

      {(component.businessView || component.technicalView) && (
        <section className="dual-view">
          <div className="panel dual-view-card">
            <span className="eyebrow">BUSINESS VIEW</span>
            <ul className="dual-view-list">
              <li>
                副作用节点：{component.businessView?.sideEffectNodes.length ?? 0} 个
                {(component.businessView?.sideEffectNodes ?? []).map((node) => (
                  <span className="metric-chip" key={node.nodeId}>
                    {node.displayName ?? node.nodeId}
                  </span>
                ))}
              </li>
              <li>需确认节点：{component.businessView?.confirmationNodes.length ?? 0} 个</li>
              <li>
                最近成功率：
                {component.businessView?.recentSuccessRate === null ||
                component.businessView?.recentSuccessRate === undefined
                  ? '暂无运行'
                  : `${(component.businessView.recentSuccessRate * 100).toFixed(0)}% （近 ${component.businessView.recentRunCount} 次）`}
              </li>
            </ul>
          </div>
          <div className="panel dual-view-card">
            <span className="eyebrow">TECHNICAL VIEW</span>
            <ul className="dual-view-list">
              <li>节点数：{component.technicalView?.nodeCount ?? 0}</li>
              <li>能力锁定：{component.technicalView?.capabilityLocks.length ?? 0} 项</li>
              <li>MCP 锁定：{component.technicalView?.mcpLocks.length ?? 0} 项</li>
              {component.technicalView?.digest && (
                <li className="digest-line">digest：{component.technicalView.digest}</li>
              )}
            </ul>
          </div>
        </section>
      )}

      <section className="detail-grid">
        <div className="graph-panel panel">
          <div className="panel-heading">
            <div>
              <span className="eyebrow">TECHNICAL DAG</span>
              <h2>执行路径</h2>
            </div>
            <span>{component.definition.spec.nodes.length} 个节点</span>
          </div>
          <WorkflowGraph definition={component.definition} />
        </div>

        <aside className="run-form panel">
          <div className="panel-heading">
            <div>
              <span className="eyebrow">INPUT CONTRACT</span>
              <h2>运行参数</h2>
            </div>
            <Play size={18} />
          </div>
          <Form
            schema={component.definition.spec.inputSchema}
            uiSchema={component.definition.spec.uiSchema}
            validator={validator}
            formData={formData}
            onChange={(event) =>
              setFormData((event.formData as Record<string, unknown>) ?? {})
            }
            onSubmit={runComponent}
          >
            <button className="button primary wide" type="submit" disabled={submitting}>
              <Play size={15} fill="currentColor" />
              {submitting ? '正在创建运行...' : '启动工作流'}
            </button>
          </Form>
          {error && <div className="form-error">{error}</div>}
          <div className="run-notes">
            <span>
              <Clock3 size={14} /> 提交后进入持久化队列
            </span>
            <span>
              <ShieldAlert size={14} /> 副作用节点会暂停等待确认
            </span>
          </div>
        </aside>
      </section>
    </div>
  )
}
