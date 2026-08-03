import {
  AlertTriangle,
  ArrowLeft,
  Ban,
  Check,
  Clock,
  Download,
  RotateCw,
  X,
} from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { api, type Approval, type Run } from '@/api/client'
import { StatusBadge } from '@/components/StatusBadge'
import { useRunEvents, useRunStream } from '@/hooks/useRunStream'

function JsonBlock({ value }: { value: unknown }) {
  return <pre className="json-block">{JSON.stringify(value, null, 2)}</pre>
}

export default function RunDetailPage() {
  const { id } = useParams()
  const [run, setRun] = useState<Run>()
  const [error, setError] = useState('')
  const events = useRunEvents((state) => (id ? state.events[id] ?? [] : []))

  const refresh = useCallback(() => {
    if (!id) return
    api.getRun(id).then(setRun).catch((reason: Error) => setError(reason.message))
  }, [id])

  useEffect(refresh, [refresh])
  useRunStream(id, refresh)

  async function decide(approval: Approval, approved: boolean) {
    try {
      if (approved) await api.approve(approval.id, approval.snapshotDigest)
      else await api.reject(approval.id)
      refresh()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '审批操作失败')
    }
  }

  if (error && !run) return <div className="page error-panel">{error}</div>
  if (!run) return <div className="page loading-panel">正在读取持久化运行状态...</div>

  return (
    <div className="page">
      <Link className="back-link" to="/components">
        <ArrowLeft size={15} /> 返回组件
      </Link>
      <header className="run-header">
        <div>
          <span className="eyebrow">RUN / {run.id.slice(0, 8)}</span>
          <h1>运行详情</h1>
        </div>
        <div className="run-header-actions">
          <StatusBadge status={run.status} />
          {!['succeeded', 'failed', 'cancelled'].includes(run.status) && (
            <button
              className="button danger ghost"
              onClick={() => id && api.cancelRun(id).then(setRun)}
            >
              <Ban size={14} /> 取消运行
            </button>
          )}
        </div>
      </header>

      {error && <div className="error-panel">{error}</div>}
      {(run.approvals ?? [])
        .filter((item) => item.status === 'pending')
        .map((approval) => (
          <section className="approval-banner" key={approval.id}>
            <div className="approval-icon">
              <AlertTriangle size={21} />
            </div>
            <div className="approval-copy">
              <span className="eyebrow">ACTION REQUIRED</span>
              <h2>{approval.snapshot.title}</h2>
              <p>{approval.snapshot.description || '请核对本次执行快照后决定是否继续。'}</p>
              <JsonBlock value={approval.snapshot.payload} />
              {approval.snapshot.effect && (
                <span className="effect-label">副作用：{approval.snapshot.effect}</span>
              )}
            </div>
            <div className="approval-actions">
              <button className="button primary" onClick={() => decide(approval, true)}>
                <Check size={15} /> 确认继续
              </button>
              <button className="button danger ghost" onClick={() => decide(approval, false)}>
                <X size={15} /> 拒绝
              </button>
            </div>
          </section>
        ))}

      <section className="run-layout">
        <div className="panel node-panel">
          <div className="panel-heading">
            <div>
              <span className="eyebrow">NODE EXECUTION</span>
              <h2>节点状态</h2>
            </div>
            <button className="icon-button" onClick={refresh} aria-label="刷新">
              <RotateCw size={15} />
            </button>
          </div>
          <div className="node-table">
            {(run.nodes ?? []).map((node) => (
              <details className="node-row" key={`${node.id}-${node.attempt}`}>
                <summary>
                  <span className={`node-state-dot ${node.status}`} />
                  <div>
                    <strong>{node.nodeId}</strong>
                    <small>
                      {node.type} · attempt {node.attempt}
                      {typeof node.durationMs === 'number' && ` · ${node.durationMs}ms`}
                    </small>
                  </div>
                  <StatusBadge status={node.status} />
                </summary>
                <div className="node-detail">
                  {node.metrics && Object.keys(node.metrics).length > 0 && (
                    <div className="node-metrics">
                      {Object.entries(node.metrics).map(([key, value]) => (
                        <span className="metric-chip" key={key}>
                          {key}: {String(value)}
                        </span>
                      ))}
                    </div>
                  )}
                  <div>
                    <small>输入</small>
                    <JsonBlock value={node.input} />
                  </div>
                  <div>
                    <small>{node.error ? '错误' : '输出'}</small>
                    <JsonBlock value={node.error ?? node.output} />
                  </div>
                </div>
              </details>
            ))}
          </div>
        </div>

        <aside className="panel event-panel">
          <div className="panel-heading">
            <div>
              <span className="eyebrow">LIVE EVENT STREAM</span>
              <h2>实时事件</h2>
            </div>
            <span className="live-label">
              <span className="pulse-dot" /> SSE
            </span>
          </div>
          <div className="event-list">
            {events.length === 0 && (
              <div className="event-empty">
                <Clock size={16} /> 等待新事件
              </div>
            )}
            {events.map((event) => (
              <div className="event-item" key={event.seq}>
                <span>{String(event.seq).padStart(4, '0')}</span>
                <div>
                  <strong>{event.type}</strong>
                  <small>{JSON.stringify(event.payload)}</small>
                </div>
              </div>
            ))}
          </div>
        </aside>
      </section>

      {(run.output || run.error) && (
        <section className="panel result-panel">
          <div className="panel-heading">
            <h2>{run.error ? '运行错误' : '最终输出'}</h2>
            <StatusBadge status={run.status} />
          </div>
          <JsonBlock value={run.error ?? run.output} />
        </section>
      )}

      {run.dependencyVersions && (
        <section className="panel result-panel">
          <div className="panel-heading">
            <div>
              <span className="eyebrow">DEPENDENCY VERSIONS</span>
              <h2>依赖版本</h2>
            </div>
            {run.dependencyVersions.executionClass && (
              <span className="effect-label">{run.dependencyVersions.executionClass}</span>
            )}
          </div>
          <JsonBlock value={run.dependencyVersions} />
        </section>
      )}

      {(run.artifacts?.length ?? 0) > 0 && (
        <section className="panel result-panel">
          <div className="panel-heading">
            <div>
              <span className="eyebrow">RUN EVIDENCE</span>
              <h2>产物与执行证据</h2>
            </div>
          </div>
          <div className="node-table">
            {run.artifacts?.map((artifact) => (
              <a className="node-row" href={artifact.downloadUrl} key={artifact.id}>
                <Download size={15} />
                <div>
                  <strong>{artifact.name}</strong>
                  <small>
                    {artifact.kind} · {artifact.mediaType} · {artifact.size} bytes
                  </small>
                </div>
              </a>
            ))}
          </div>
        </section>
      )}
    </div>
  )
}
