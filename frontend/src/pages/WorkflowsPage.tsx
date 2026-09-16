import { Button, Input, Skeleton } from 'antd'
import { Cable, FileCode2, Plus, ShieldCheck } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import { api, type Workflow } from '@/api/client'
import { defaultWorkflow } from '@/lib/defaultWorkflow'

export default function WorkflowsPage() {
  const navigate = useNavigate()
  const [workflows, setWorkflows] = useState<Workflow[]>([])
  const [error, setError] = useState('')
  const [query, setQuery] = useState('')
  const [loading, setLoading] = useState(true)
  const [creating, setCreating] = useState(false)

  useEffect(() => {
    api.listWorkflows().then(setWorkflows).catch((reason: Error) => setError(reason.message)).finally(() => setLoading(false))
  }, [])

  async function createWorkflow() {
    setCreating(true)
    try {
      const workflow = await api.createWorkflow('问候工作流', defaultWorkflow)
      navigate(`/workflows/${workflow.id}/edit`)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '创建失败')
    } finally {
      setCreating(false)
    }
  }

  return (
    <div className="page">
      <header className="page-header">
        <div>
          <span className="eyebrow">WORKFLOW AUTHORING</span>
          <h1>工作流草稿</h1>
          <p>编辑显式 DAG、输入输出契约和执行策略，发布后版本不可变。</p>
        </div>
        <Button className="button primary" onClick={createWorkflow} disabled={creating}>
          <Plus size={15} /> {creating ? '正在创建...' : '新建示例'}
        </Button>
      </header>
      <div className="collection-toolbar"><Input.Search aria-label="搜索工作流" placeholder="搜索工作流" allowClear value={query} onChange={event => setQuery(event.target.value)} style={{ maxWidth: 300 }} /><span className="hint">{workflows.length} 个草稿</span></div>
      {loading && <Skeleton active />}
      {error && <div className="error-panel">{error}</div>}
      {!loading && workflows.length === 0 && !error ? (
        <section className="empty-state">
          <Cable size={30} />
          <h2>尚无工作流草稿</h2>
          <p>从一个可运行的确定性示例开始，随后修改 DSL 并执行静态校验。</p>
          <Button className="button primary" onClick={createWorkflow}>
            创建首个草稿
          </Button>
        </section>
      ) : (
        <section className="workflow-list">
          {workflows.filter(workflow => workflow.name.toLowerCase().includes(query.toLowerCase())).map((workflow) => (
            <Link
              className="workflow-list-item"
              to={`/workflows/${workflow.id}/edit`}
              key={workflow.id}
            >
              <span className="component-icon">
                <FileCode2 size={17} />
              </span>
              <div>
                <strong>{workflow.name}</strong>
                <small>{String(workflow.definition.metadata.name ?? 'unnamed')}</small>
              </div>
              <span className="revision-label">REV {workflow.draftRevision}</span>
              <span className="contract-label">
                <ShieldCheck size={13} /> Schema
              </span>
            </Link>
          ))}
        </section>
      )}
    </div>
  )
}
