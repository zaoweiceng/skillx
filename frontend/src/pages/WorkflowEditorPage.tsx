import { Button } from 'antd'
import Form from '@/components/SchemaForm'
import validator from '@rjsf/validator-ajv8'
import { ArrowLeft, CheckCircle2, Code2, Play, Plus, Save, Send } from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'

import {
  api,
  type NodeDefinition,
  type Run,
  type Workflow,
  type WorkflowDefinition,
  type WorkflowVersionSummary,
} from '@/api/client'
import { EditableCanvas } from '@/components/EditableCanvas'
import { outputVariables, reachableUpstream } from '@/lib/variablePicker'
import {
  addNode,
  connectNodes,
  deleteNode,
  disconnectNodes,
  PALETTE_NODE_TYPES,
  type PaletteNodeType,
  setInputMapping,
  updateNode,
} from '@/lib/workflowEdit'
import { rjsfSchema } from '@/lib/utils'
import { NodeInspector } from '@/features/workflows/NodeInspector'
import { CapabilityMetadataEditor } from '@/features/workflows/CapabilityMetadataEditor'

type Diagnostic = { level: string; code: string; path: string; message: string }

export default function WorkflowEditorPage() {
  const { id } = useParams()
  const navigate = useNavigate()
  const [workflow, setWorkflow] = useState<Workflow>()
  // 结构化 state 为唯一真相；JSON 仅作为高级导入/导出视图。
  const [definition, setDefinition] = useState<WorkflowDefinition>()
  const [showJson, setShowJson] = useState(false)
  const [jsonDraft, setJsonDraft] = useState('')
  const [diagnostics, setDiagnostics] = useState<Diagnostic[]>([])
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [publishing, setPublishing] = useState(false)
  const [selectedNodeId, setSelectedNodeId] = useState('')
  const [mappingField, setMappingField] = useState('')
  const [versions, setVersions] = useState<WorkflowVersionSummary[]>([])
  const [versionRuns, setVersionRuns] = useState<Run[]>([])
  const [runInput, setRunInput] = useState<Record<string, unknown>>({})

  useEffect(() => {
    if (!id) return
    api
      .getWorkflow(id)
      .then((item) => {
        setWorkflow(item)
        setDefinition(item.definition)
        if (typeof api.listWorkflowVersions === 'function') {
          api.listWorkflowVersions(item.id).then(async (published) => {
            setVersions(published)
            if (published[0] && typeof api.listRuns === 'function') {
              const history = await api.listRuns({
                workflowVersionId: published[0].id,
                limit: 20,
              })
              setVersionRuns(history.items)
            }
          })
        }
      })
      .catch((reason: Error) => setError(reason.message))
  }, [id])

  const selectedNode = useMemo(
    () => definition?.spec.nodes.find((node) => node.id === selectedNodeId),
    [definition, selectedNodeId],
  )

  const upstreamVariables = useMemo(() => {
    if (!definition || !selectedNodeId) return []
    return reachableUpstream(definition, selectedNodeId).flatMap((nodeId) =>
      outputVariables(definition, nodeId),
    )
  }, [definition, selectedNodeId])

  const riskSummary = useMemo(() => {
    if (!definition) return []
    return definition.spec.nodes
      .filter((node) => node.sideEffect || ['approval', 'browser'].includes(node.type))
      .map((node) => `${node.displayName}（${node.type}）需要确认或存在副作用`)
  }, [definition])

  // fallback target 只能是 agent 节点：预先计算可选目标列表，供 Inspector 选择。
  const agentTargets = useMemo(
    () => (definition?.spec.nodes ?? []).filter((node) => node.type === 'agent').map((n) => n.id),
    [definition],
  )

  const patchDefinition = useCallback((next: WorkflowDefinition) => {
    setDefinition(next)
    setNotice('')
  }, [])

  function handleAddNode(type: PaletteNodeType) {
    if (!definition) return
    const { definition: next, nodeId } = addNode(definition, type)
    patchDefinition(next)
    setSelectedNodeId(nodeId)
  }

  function handleConnect(source: string, target: string) {
    if (!definition) return
    const result = connectNodes(definition, source, target)
    if (!result.ok) {
      setError(result.error)
      return
    }
    setError('')
    patchDefinition(result.definition)
  }

  function handleDeleteNode(nodeId: string) {
    if (!definition) return
    const result = deleteNode(definition, nodeId)
    if (!result.ok) {
      setError(result.error)
      return
    }
    setError('')
    patchDefinition(result.definition)
    if (selectedNodeId === nodeId) setSelectedNodeId('')
  }

  function handleDeleteEdge(source: string, target: string) {
    if (!definition) return
    patchDefinition(disconnectNodes(definition, source, target))
  }

  function patchNode(patch: Partial<NodeDefinition>) {
    if (!definition || !selectedNodeId) return
    patchDefinition(updateNode(definition, selectedNodeId, patch))
  }

  function insertMapping(expression: string) {
    if (!definition || !selectedNodeId || !mappingField) {
      setError('请先填写要写入的输入字段名')
      return
    }
    setError('')
    patchDefinition(setInputMapping(definition, selectedNodeId, mappingField, expression))
  }

  async function save(): Promise<Workflow | undefined> {
    if (!workflow || !id || !definition) return
    setBusy(true)
    setError('')
    try {
      const updated = await api.updateDraft(id, workflow.draftRevision, definition)
      setWorkflow(updated)
      setNotice(`草稿已保存，修订号 ${updated.draftRevision}`)
      return updated
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '保存失败')
    } finally {
      setBusy(false)
    }
  }

  async function validate(): Promise<{ valid: boolean; saved?: Workflow }> {
    const saved = await save()
    if (!saved || !id) return { valid: false }
    const result = await api.validateWorkflow(id)
    setDiagnostics(result.diagnostics ?? [])
    setNotice(result.valid ? `静态检查通过 · ${result.digest}` : '静态检查未通过')
    return { valid: result.valid, saved }
  }

  async function publish() {
    if (publishing) return
    setPublishing(true)
    try {
      const validation = await validate()
      if (!id || !validation.saved || !validation.valid) return
      const reviewed = validation.saved
      const version = await api.publishWorkflow(
        id,
        reviewed.draftRevision,
        reviewed.draftDigest,
        reviewed.draftMetadata?.replayEvidenceId,
      )
      const component = await api.createComponent(
        reviewed.name,
        '由工作流编辑器发布的锁定版本组件。',
        version.id,
      )
      setNotice(
        `已发布不可变版本 v${version.version}，组件入口 ${component.id.slice(0, 8)} 已创建`,
      )
      if (typeof api.listWorkflowVersions === 'function') {
        setVersions(await api.listWorkflowVersions(id))
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '发布失败')
    } finally {
      setPublishing(false)
    }
  }

  function toggleJson() {
    if (!definition) return
    if (!showJson) {
      setJsonDraft(JSON.stringify(definition, null, 2))
      setShowJson(true)
      return
    }
    // 从 JSON 视图导入回结构化 state。
    try {
      const parsed = JSON.parse(jsonDraft) as WorkflowDefinition
      setDefinition(parsed)
      setShowJson(false)
      setError('')
    } catch {
      setError('JSON 语法错误，无法导入')
    }
  }

  async function runPublishedVersion() {
    const version = versions[0]
    if (!version) return
    setBusy(true)
    try {
      const run = await api.createWorkflowVersionRun(version.id, runInput)
      navigate(`/runs/${run.id}`)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '运行已发布版本失败')
    } finally {
      setBusy(false)
    }
  }

  if (!workflow || !definition) {
    return <div className="page loading-panel">{error || '正在加载工作流草稿...'}</div>
  }

  return (
    <div className="page editor-page">
      <Link className="back-link" to="/workflows">
        <ArrowLeft size={15} /> 返回工作流
      </Link>
      <header className="run-header">
        <div>
          <span className="eyebrow">DRAFT REVISION {workflow.draftRevision}</span>
          <h1>{workflow.name}</h1>
        </div>
        <div className="run-header-actions">
          <Button className="button ghost" onClick={toggleJson} disabled={busy || publishing}>
            <Code2 size={15} /> {showJson ? '应用 JSON' : 'JSON 视图'}
          </Button>
          <Button className="button ghost" onClick={save} disabled={busy || publishing}>
            <Save size={15} /> 保存
          </Button>
          <Button className="button ghost" onClick={validate} disabled={busy || publishing}>
            <CheckCircle2 size={15} /> 静态检查
          </Button>
          <Button className="button primary" onClick={publish} disabled={busy || publishing}>
            <Send size={15} /> {publishing ? '发布中...' : '发布版本'}
          </Button>
        </div>
      </header>

      {(error || notice) && (
        <div className={error ? 'error-panel' : 'notice-panel'}>{error || notice}</div>
      )}

      {!showJson && (
        <CapabilityMetadataEditor
          definition={definition}
          onChange={patchDefinition}
          selectedNodeId={selectedNodeId}
        />
      )}

      {showJson ? (
        <section className="panel source-panel">
          <div className="panel-heading">
            <div>
              <span className="eyebrow">WORKFLOW DSL</span>
              <h2>定义 JSON（高级导入/导出）</h2>
            </div>
          </div>
          <textarea
            aria-label="工作流 JSON 定义"
            className="source-editor"
            spellCheck={false}
            value={jsonDraft}
            onChange={(event) => setJsonDraft(event.target.value)}
          />
        </section>
      ) : (
        <section className="editor-layout">
          <div className="panel palette-panel">
            <div className="panel-heading">
              <span className="eyebrow">NODE PALETTE</span>
            </div>
            <div className="palette-list">
              {PALETTE_NODE_TYPES.map((type) => (
                <Button
                  key={type}
                  className="palette-item"
                  htmlType="button"
                  onClick={() => handleAddNode(type)}
                >
                  <Plus size={13} /> {type}
                </Button>
              ))}
            </div>
          </div>
          <div className="panel preview-panel">
            <div className="panel-heading">
              <div>
                <span className="eyebrow">CANVAS</span>
                <h2>可视化编辑</h2>
              </div>
              <span className="hint">拖拽连线 · Delete 删除选中节点/连线</span>
            </div>
            <EditableCanvas
              definition={definition}
              selectedNodeId={selectedNodeId}
              onSelectNode={setSelectedNodeId}
              onConnect={handleConnect}
              onDeleteNode={handleDeleteNode}
              onDeleteEdge={handleDeleteEdge}
            />
            <div className="diagnostics">
              {diagnostics.map((item) => (
                <div className={`diagnostic ${item.level}`} key={`${item.code}-${item.path}`}>
                  <strong>{item.code}</strong>
                  <span>{item.message}</span>
                  <small>{item.path}</small>
                </div>
              ))}
            </div>
            {riskSummary.length > 0 && (
              <div className="risk-summary">
                <span className="eyebrow">PRE-PUBLISH RISK</span>
                <ul>
                  {riskSummary.map((line) => (
                    <li key={line}>{line}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>
          <div className="panel inspector-panel">
            <div className="panel-heading">
              <span className="eyebrow">NODE INSPECTOR</span>
            </div>
            {!selectedNode ? (
              <p className="inspector-empty">在画布中选择一个节点以编辑其契约。</p>
            ) : (
              <NodeInspector
                node={selectedNode}
                variables={upstreamVariables}
                agentTargets={agentTargets}
                mappingField={mappingField}
                onMappingFieldChange={setMappingField}
                onPatch={patchNode}
                onInsertMapping={insertMapping}
              />
            )}
          </div>
        </section>
      )}

      <section className="detail-grid published-version-panel">
        <div className="panel result-panel">
          <div className="panel-heading">
            <div>
              <span className="eyebrow">PUBLISHED HISTORY</span>
              <h2>已发布版本与运行历史</h2>
            </div>
            <span>{versions.length} 个版本</span>
          </div>
          {versions.length === 0 ? (
            <p className="inspector-empty">尚未发布；草稿不能进入正式运行历史。</p>
          ) : (
            <div className="node-table">
              {versions.map((version) => (
                <div className="node-row" key={version.id}>
                  <div>
                    <strong>v{version.version}</strong>
                    <small>{new Date(version.createdAt).toLocaleString()}</small>
                    <code className="digest-line">{version.digest}</code>
                  </div>
                </div>
              ))}
              {versionRuns.map((run) => (
                <Link className="node-row" to={`/runs/${run.id}`} key={run.id}>
                  <span className={`status-badge ${run.status}`}>{run.status}</span>
                  <div>
                    <strong>{new Date(run.createdAt).toLocaleString()}</strong>
                    <small>{run.durationMs ?? '待完成'}ms</small>
                  </div>
                </Link>
              ))}
            </div>
          )}
        </div>
        {versions[0] && (
          <aside className="panel run-form">
            <div className="panel-heading">
              <div>
                <span className="eyebrow">RUN PUBLISHED VERSION</span>
                <h2>手动运行 v{versions[0].version}</h2>
              </div>
              <Play size={16} />
            </div>
            <Form
              schema={rjsfSchema(versions[0].definition.spec.inputSchema)}
              uiSchema={versions[0].definition.spec.uiSchema}
              validator={validator}
              formData={runInput}
              onChange={(event) =>
                setRunInput((event.formData as Record<string, unknown>) ?? {})
              }
              onSubmit={runPublishedVersion}
            >
              <Button className="button primary wide" htmlType="submit" disabled={busy}>
                <Play size={14} /> 启动已发布版本
              </Button>
            </Form>
          </aside>
        )}
      </section>
    </div>
  )
}
