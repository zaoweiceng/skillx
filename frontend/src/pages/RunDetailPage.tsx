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
  // WorkflowPatch 治理：应用受控补丁 → 展示 replay diagnostics → 打开生成的 draft。
  const [patchDraft, setPatchDraft] = useState<{
    draftWorkflowId: string
    diagnostics: Array<{ level: string; code: string; path: string; message: string }>
    publishable: boolean
  }>()
  const [patching, setPatching] = useState(false)

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

  // 对失败节点提出受控补丁：把超时上调，作为可复现故障的最小修复示例。
  // 补丁只创建 draft（旧版本不变），并回放展示 diagnostics，人工发布后才生效。
  async function proposePatch(failedNode: string) {
    if (!run?.workflowVersionId) {
      setError('临时计划不能直接修改源版本，请先人工固化为 Workflow draft。')
      return
    }
    setPatching(true)
    setError('')
    try {
      const failed = (run.nodes ?? []).find((node) => node.nodeId === failedNode)
      const currentTimeout =
        typeof failed?.input?.timeoutSeconds === 'number' ? failed.input.timeoutSeconds : 30
      const result = await api.applyWorkflowPatch(run.workflowVersionId, {
        sourceRunId: run.id,
        patch: {
          nodePatches: [
            {
              nodeId: failedNode,
              changes: { timeoutSeconds: Math.min(currentTimeout * 2, 86400) },
            },
          ],
          reason: `对失败节点 ${failedNode} 提出受控超时补丁`,
        },
      })
      setPatchDraft(result)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '应用补丁失败')
    } finally {
      setPatching(false)
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
          {run.planSnapshotId && (
            <p className="run-notes">
              临时 PlanSnapshot · {run.planSnapshotId.slice(0, 8)} · 冻结版本不随源默认版本变化
            </p>
          )}
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

      {run.status === 'failed' && (run.nodes ?? []).some((node) => node.status === 'failed') && (
        <section className="panel result-panel">
          <div className="panel-heading">
            <div>
              <span className="eyebrow">PATCH GOVERNANCE</span>
              <h2>受控版本修订</h2>
            </div>
          </div>
          <p className="run-notes">
            对可复现失败提出受控 WorkflowPatch：只创建 draft、回放校验并展示 diff，旧版本不变，
            人工发布后才成为新版本。
          </p>
          {(run.nodes ?? [])
            .filter((node) => node.status === 'failed')
            .map((node) => (
              <div className="mapping-row" key={node.id}>
                <span className="metric-chip">失败节点 {node.nodeId}</span>
                <button
                  className="button ghost"
                  type="button"
                  disabled={patching}
                  onClick={() => proposePatch(node.nodeId)}
                >
                  {patching ? '正在生成补丁草稿...' : '提出补丁草稿'}
                </button>
              </div>
            ))}
          {patchDraft && (
            <div className="notice-panel">
              <p>
                已生成补丁草稿，回放{patchDraft.publishable ? '通过' : '存在阻断项'}。
                <Link className="button ghost" to={`/workflows/${patchDraft.draftWorkflowId}/edit`}>
                  打开草稿审核 diff 并发布
                </Link>
              </p>
              {patchDraft.diagnostics.length > 0 && (
                <div className="diagnostics">
                  {patchDraft.diagnostics.map((item) => (
                    <div className={`diagnostic ${item.level}`} key={`${item.code}-${item.path}`}>
                      <strong>{item.code}</strong>
                      <span>{item.message}</span>
                      <small>{item.path}</small>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
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

      {(run.checkpoints?.length ?? 0) > 0 && (
        <section className="panel result-panel">
          <div className="panel-heading">
            <div>
              <span className="eyebrow">CHECKPOINT RECOVERY</span>
              <h2>检查点接力</h2>
            </div>
          </div>
          <div className="node-table">
            {run.checkpoints?.map((checkpoint) => (
              <details className="node-row" key={checkpoint.id}>
                <summary>
                  <span className={`node-state-dot ${checkpoint.status}`} />
                  <div>
                    <strong>失败节点 {checkpoint.failedNode}</strong>
                    <small>
                      attempt {checkpoint.attempt} · 剩余子图 {checkpoint.remainingSubgraph.length}{' '}
                      个节点
                      {checkpoint.fallbackTarget ? ` · fallback → ${checkpoint.fallbackTarget}` : ''}
                      {checkpoint.approvalStatus && checkpoint.approvalStatus !== 'none'
                        ? ` · 审批 ${checkpoint.approvalStatus}`
                        : ''}
                    </small>
                  </div>
                  <StatusBadge status={checkpoint.status} />
                </summary>
                <div className="node-detail">
                  <div>
                    <small>已完成节点（冻结输出，不重放）</small>
                    <div className="current-mapping">
                      {checkpoint.completedNodes.map((nodeId) => (
                        <code className="variable-chip" key={nodeId}>
                          {nodeId}
                        </code>
                      ))}
                    </div>
                  </div>
                  <div>
                    <small>已提交非幂等副作用（去重边界）</small>
                    <JsonBlock value={checkpoint.committedSideEffects} />
                  </div>
                  <div>
                    <small>剩余子图（交给受限 Agent 接力）</small>
                    <JsonBlock value={checkpoint.remainingSubgraph} />
                  </div>
                  {checkpoint.decision && (
                    <div>
                      <small>恢复决定</small>
                      <JsonBlock value={checkpoint.decision} />
                    </div>
                  )}
                </div>
              </details>
            ))}
          </div>
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
