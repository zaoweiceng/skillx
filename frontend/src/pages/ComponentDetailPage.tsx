import { Alert, Button, Card, Collapse, Descriptions, Empty, Flex, Tag, Typography } from 'antd'
import Form from '@/components/SchemaForm'
import validator from '@rjsf/validator-ajv8'
import { ArrowLeft, Clock3, Play, ShieldAlert } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'

import { api, type ComponentSummary, type Run } from '@/api/client'
import { WorkflowGraph } from '@/components/WorkflowGraph'
import { rjsfSchema } from '@/lib/utils'

export default function ComponentDetailPage() {
  const { id } = useParams()
  const navigate = useNavigate()
  const [component, setComponent] = useState<ComponentSummary>()
  const [formData, setFormData] = useState<Record<string, unknown>>({})
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [runs, setRuns] = useState<Run[]>([])
  const [runsCursor, setRunsCursor] = useState<string | null>(null)

  useEffect(() => {
    if (!id) return
    Promise.all([
      api.getComponent(id),
      api.listRuns({ componentId: id, limit: 20 }),
    ])
      .then(([item, history]) => {
        setComponent(item)
        setRuns(history.items)
        setRunsCursor(history.nextCursor)
      })
      .catch((reason: Error) => setError(reason.message))
  }, [id])

  async function loadMoreRuns() {
    if (!id || !runsCursor) return
    const page = await api.listRuns({ componentId: id, cursor: runsCursor, limit: 20 })
    setRuns((current) => [...current, ...page.items])
    setRunsCursor(page.nextCursor)
  }

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

  const manifest = component.capabilityManifest
  const business = component.businessView
  const technical = component.technicalView
  return (
    <div className="page detail-page component-detail-page">
      <Link className="back-link" to="/components"><ArrowLeft size={15} /> 返回组件</Link>
      <header className="page-header compact-header">
        <div><span className="eyebrow">工作空间 / 组件</span><h1>{component.name}</h1><p>{component.description || '填写参数，启动已发布的工作流。'}</p></div>
        <Tag>{{zero_llm: '零 LLM', hybrid: '局部 LLM', agent: 'Agent 工作流'}[component.executionClass]}</Tag>
      </header>

      {(business || technical) && <section className="component-summary-grid">
        <Card title="执行概览" size="small">
          <Descriptions column={1} size="small" items={[
            {key: 'effects', label: '副作用节点', children: <Flex gap={6} wrap><span>{business?.sideEffectNodes.length ?? 0} 个</span>{business?.sideEffectNodes.map(node => <Tag key={node.nodeId}>{node.displayName ?? node.nodeId}</Tag>)}</Flex>},
            {key: 'confirmations', label: '需确认节点', children: `${business?.confirmationNodes.length ?? 0} 个`},
            {key: 'rate', label: '最近成功率', children: business?.recentSuccessRate == null ? '暂无运行' : `${(business.recentSuccessRate * 100).toFixed(0)}%（近 ${business.recentRunCount} 次）`},
          ]} />
        </Card>
        <Card title="版本信息" size="small">
          <Descriptions column={1} size="small" items={[
            {key: 'nodes', label: '执行节点', children: `${technical?.nodeCount ?? component.definition.spec.nodes.length} 个`},
            {key: 'capabilities', label: '能力锁定', children: `${technical?.capabilityLocks.length ?? 0} 项`},
            {key: 'mcp', label: 'MCP 锁定', children: `${technical?.mcpLocks.length ?? 0} 项`},
          ]} />
        </Card>
      </section>}

      {manifest && <Card title="能力说明" className="component-section" extra={<Tag>{manifest.entrypoints.join(' · ')}</Tag>}>
        <Typography.Paragraph>{manifest.summary}</Typography.Paragraph>
        <Flex gap={6} wrap className="manifest-tags">{manifest.tags.map(tag => <Tag key={tag}>{tag}</Tag>)}</Flex>
        <Descriptions column={1} size="small" items={[
          {key: 'use', label: '适用场景', children: manifest.useWhen.join('；') || '未补充'},
          {key: 'avoid', label: '不适用场景', children: manifest.doNotUseWhen.join('；') || '未补充'},
        ]} />
        {(manifest.fragments?.length ?? 0) > 0 && <Flex vertical gap={12} className="manifest-fragments">
          {manifest.fragments?.map(fragment => <Card size="small" key={fragment.fragmentRef} title={fragment.name}>
            <Typography.Paragraph>{fragment.summary}</Typography.Paragraph>
            <Typography.Text type="secondary" className="digest-line">{fragment.fragmentRef}</Typography.Text>
          </Card>)}
        </Flex>}
      </Card>}

      <section className="detail-grid component-section">
        <Card title={<h2>执行路径</h2>} extra={<Typography.Text type="secondary">{component.definition.spec.nodes.length} 个节点</Typography.Text>}
          className="component-graph-card" styles={{body: {padding: 0}}}>
          <WorkflowGraph definition={component.definition} />
        </Card>
        <Card title={<h2>运行参数</h2>} className="component-run-card">
          <Form schema={rjsfSchema(component.definition.spec.inputSchema)} uiSchema={component.definition.spec.uiSchema}
            validator={validator} formData={formData} disabled={submitting}
            onChange={event => setFormData((event.formData as Record<string, unknown>) ?? {})} onSubmit={runComponent}>
            <Button type="primary" block htmlType="submit" loading={submitting} icon={<Play size={15} />}>
              {submitting ? '正在创建运行...' : '启动工作流'}
            </Button>
          </Form>
          {error && <Alert type="error" title={error} showIcon />}
          <Flex vertical gap={8} className="component-run-notes">
            <span><Clock3 size={14} /> 提交后进入执行队列</span>
            <span><ShieldAlert size={14} /> 副作用节点会暂停等待确认</span>
          </Flex>
        </Card>
      </section>

      {(technical?.digest || manifest) && <Collapse className="component-section" items={[{key: 'contract', label: '版本与契约详情', children:
        <Descriptions column={1} size="small" items={[
          ...(technical?.digest ? [{key: 'digest', label: '版本摘要', children: <Typography.Text className="digest-line" copyable>{technical.digest}</Typography.Text>}] : []),
          ...(manifest ? [
            {key: 'ref', label: '工作流引用', children: <Typography.Text className="digest-line" copyable>{manifest.workflowRef}</Typography.Text>},
            {key: 'manifest', label: '清单摘要', children: <Typography.Text className="digest-line" copyable>{manifest.manifestDigest}</Typography.Text>},
          ] : []),
        ]} />,
      }]} />}

      <Card title={<h2>调用历史</h2>} extra={<Typography.Text type="secondary">{runs.length} 条已加载</Typography.Text>} className="component-section">
        {runs.length === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无调用记录" /> : <div className="node-table">
          {runs.map(run => <Link className="node-row run-history-row" to={`/runs/${run.id}`} key={run.id}>
            <Tag>{run.status}</Tag>
            <div><strong>{new Date(run.createdAt).toLocaleString()}</strong>
              <small>{run.durationMs == null ? '耗时待定' : `${run.durationMs}ms`}{' · '}version {run.workflowVersionId?.slice(0, 8)}{' · '}Presentation {run.presentation?.status ?? '未启用'}</small>
              <code className="digest-line">输入 {JSON.stringify(run.input)?.slice(0, 100)}{' · '}输出 {JSON.stringify(run.output)?.slice(0, 100)}</code>
            </div>
          </Link>)}
        </div>}
        {runsCursor && <Button onClick={loadMoreRuns}>加载更多</Button>}
      </Card>
    </div>
  )
}
