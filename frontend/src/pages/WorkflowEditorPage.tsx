import Form from '@rjsf/core'
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
          <button className="button ghost" onClick={toggleJson} disabled={busy || publishing}>
            <Code2 size={15} /> {showJson ? '应用 JSON' : 'JSON 视图'}
          </button>
          <button className="button ghost" onClick={save} disabled={busy || publishing}>
            <Save size={15} /> 保存
          </button>
          <button className="button ghost" onClick={validate} disabled={busy || publishing}>
            <CheckCircle2 size={15} /> 静态检查
          </button>
          <button className="button primary" onClick={publish} disabled={busy || publishing}>
            <Send size={15} /> {publishing ? '发布中...' : '发布版本'}
          </button>
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
                <button
                  key={type}
                  className="palette-item"
                  type="button"
                  onClick={() => handleAddNode(type)}
                >
                  <Plus size={13} /> {type}
                </button>
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
              <button className="button primary wide" type="submit" disabled={busy}>
                <Play size={14} /> 启动已发布版本
              </button>
            </Form>
          </aside>
        )}
      </section>
    </div>
  )
}

function CapabilityMetadataEditor({
  definition,
  onChange,
  selectedNodeId,
}: {
  definition: WorkflowDefinition
  onChange: (definition: WorkflowDefinition) => void
  selectedNodeId: string
}) {
  const capability =
    typeof definition.metadata.capability === 'object' && definition.metadata.capability
      ? (definition.metadata.capability as Record<string, unknown>)
      : {}
  const fragments = Array.isArray(definition.metadata.callableFragments)
    ? (definition.metadata.callableFragments as Array<Record<string, unknown>>)
    : []
  const presentation =
    typeof definition.metadata.presentationPolicy === 'object' &&
    definition.metadata.presentationPolicy
      ? (definition.metadata.presentationPolicy as Record<string, unknown>)
      : {}
  const presentationProposal =
    typeof definition.metadata.presentationPolicyProposal === 'object' &&
    definition.metadata.presentationPolicyProposal
      ? (definition.metadata.presentationPolicyProposal as Record<string, unknown>)
      : {}
  const presentationDecision =
    typeof definition.metadata.presentationPolicyDecision === 'object' &&
    definition.metadata.presentationPolicyDecision
      ? (definition.metadata.presentationPolicyDecision as Record<string, unknown>)
      : {}
  const presentationDraft =
    Object.keys(presentation).length > 0 ? presentation : presentationProposal

  function patchMetadata(nextCapability: Record<string, unknown>, nextFragments = fragments) {
    onChange({
      ...definition,
      metadata: {
        ...definition.metadata,
        capability: nextCapability,
        callableFragments: nextFragments,
      },
    })
  }

  function patchPresentation(patch: Record<string, unknown>) {
    onChange({
      ...definition,
      metadata: {
        ...definition.metadata,
        presentationPolicy: { ...presentationDraft, ...patch },
        presentationPolicyDecision: { status: 'pending' },
      },
    })
  }

  function decidePresentation(status: 'accepted' | 'modified' | 'rejected') {
    const policy =
      status === 'rejected'
        ? {
            enabled: false,
            audience: 'internal',
            producer: 'template',
            template: '执行结果：{json}',
            prompt: String(presentationDraft.prompt ?? ''),
            language: String(presentationDraft.language ?? 'zh-CN'),
            maxLength: Number(presentationDraft.maxLength ?? 2000),
          }
        : {
            enabled: presentationDraft.enabled === true,
            audience: presentationDraft.enabled === true ? 'end_user' : 'internal',
            producer: String(presentationDraft.producer ?? 'template'),
            template: String(presentationDraft.template ?? '执行结果：{json}'),
            prompt: String(presentationDraft.prompt ?? ''),
            language: String(presentationDraft.language ?? 'zh-CN'),
            maxLength: Number(presentationDraft.maxLength ?? 2000),
          }
    onChange({
      ...definition,
      metadata: {
        ...definition.metadata,
        presentationPolicy: policy,
        presentationPolicyDecision: {
          status,
          reviewer: 'local-user',
          reviewedAt: new Date().toISOString(),
        },
      },
    })
  }

  function insertPresentationField(field: string) {
    const current = String(presentationDraft.template ?? '执行结果：{json}')
    patchPresentation({ template: `${current}{${field}}` })
  }

  function listValue(key: string) {
    return Array.isArray(capability[key]) ? (capability[key] as unknown[]).join(', ') : ''
  }

  function patchList(key: string, value: string) {
    patchMetadata({
      ...capability,
      [key]: value
        .split(',')
        .map((item) => item.trim())
        .filter(Boolean),
    })
  }

  function addFragment() {
    if (!selectedNodeId) return
    const sourceProperties = definition.spec.inputSchema.properties as
      | Record<string, unknown>
      | undefined
    const sourceField = Object.keys(sourceProperties ?? {})[0] ?? 'input'
    const next = [
      ...fragments,
      {
        name: `fragment-${fragments.length + 1}`,
        summary: '',
        nodeIds: [selectedNodeId],
        inputSchema: definition.spec.inputSchema,
        outputSchema: {
          type: 'object',
          properties: { result: {} },
          required: ['result'],
        },
        inputMapping: { [sourceField]: `\${input.${sourceField}}` },
        outputMapping: { result: `\${${selectedNodeId}.output}` },
      },
    ]
    patchMetadata(capability, next)
  }

  return (
    <section className="panel capability-metadata-panel">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">CAPABILITY CONTRACT</span>
          <h2>目录元数据与 Fragment 端口</h2>
        </div>
        <button className="button ghost" type="button" onClick={addFragment} disabled={!selectedNodeId}>
          <Plus size={13} /> 将选中节点声明为 Fragment
        </button>
      </div>
      <div className="inspector-fields">
        <label>
          <span>能力摘要</span>
          <input
            value={String(capability.summary ?? '')}
            onChange={(event) => patchMetadata({ ...capability, summary: event.target.value })}
          />
        </label>
        <label>
          <span>适用场景（逗号分隔）</span>
          <input
            value={listValue('useWhen')}
            onChange={(event) => patchList('useWhen', event.target.value)}
          />
        </label>
        <label>
          <span>不适用场景（逗号分隔）</span>
          <input
            value={listValue('doNotUseWhen')}
            onChange={(event) => patchList('doNotUseWhen', event.target.value)}
          />
        </label>
        <label>
          <span>示例（逗号分隔）</span>
          <input
            value={listValue('examples')}
            onChange={(event) => patchList('examples', event.target.value)}
          />
        </label>
        <label>
          <span>标签（逗号分隔）</span>
          <input
            value={listValue('tags')}
            onChange={(event) => patchList('tags', event.target.value)}
          />
        </label>
        <div className="presentation-policy-editor">
          {Object.keys(presentationProposal).length > 0 && (
            <div className="notice-panel presentation-proposal-evidence">
              <strong>固化 Agent 提议</strong>
              <span>
                来源：{String(presentationProposal.source ?? 'unknown')} · 受众：
                {String(presentationProposal.resultAudience ?? 'unknown')} · 消费者：
                {String(presentationProposal.consumer ?? 'unknown')} · 位置：
                {String(presentationProposal.terminalOrFragment ?? 'unknown')} · 置信度：
                {Number(presentationProposal.confidence ?? 0).toFixed(2)}
              </span>
              <span>{String(presentationProposal.reason ?? '')}</span>
              {Array.isArray(presentationProposal.evidence) && (
                <ul>
                  {presentationProposal.evidence.map((item) => (
                    <li key={String(item)}>{String(item)}</li>
                  ))}
                </ul>
              )}
              <span>审阅状态：{String(presentationDecision.status ?? 'pending')}</span>
            </div>
          )}
          <label>
            <input
              type="checkbox"
              checked={presentationDraft.enabled === true}
              onChange={(event) =>
                patchPresentation({
                  enabled: event.target.checked,
                  audience: event.target.checked ? 'end_user' : 'internal',
                })
              }
            />
            <span>面向最终用户生成阅读结果</span>
          </label>
          <label>
            <span>生成方式</span>
            <select
              value={String(presentationDraft.producer ?? 'template')}
              onChange={(event) => patchPresentation({ producer: event.target.value })}
            >
              <option value="template">稳定模板（零 LLM）</option>
              <option value="llm">LLM 整理</option>
            </select>
          </label>
          {String(presentationDraft.producer ?? 'template') === 'template' && (
            <>
              <label>
                <span>模板（支持 {'{json}'} 和顶层字段）</span>
                <input
                  value={String(presentationDraft.template ?? '执行结果：{json}')}
                  onChange={(event) => patchPresentation({ template: event.target.value })}
                />
              </label>
              <div className="presentation-field-chips" aria-label="可插入输出字段">
                {['json', ...Object.keys(
                  (definition.spec.outputSchema.properties as Record<string, unknown>) ?? {},
                )].map((field) => (
                  <button
                    className="metric-chip"
                    type="button"
                    key={field}
                    onClick={() => insertPresentationField(field)}
                  >
                    {`{${field}}`}
                  </button>
                ))}
              </div>
            </>
          )}
          {String(presentationDraft.producer ?? 'template') === 'llm' && (
            <label>
              <span>LLM Prompt</span>
              <textarea
                value={String(presentationDraft.prompt ?? '')}
                onChange={(event) => patchPresentation({ prompt: event.target.value })}
              />
            </label>
          )}
          <label>
            <span>语言</span>
            <input
              value={String(presentationDraft.language ?? 'zh-CN')}
              onChange={(event) => patchPresentation({ language: event.target.value })}
            />
          </label>
          <label>
            <span>最大长度</span>
            <input
              type="number"
              min={64}
              max={8000}
              value={Number(presentationDraft.maxLength ?? 2000)}
              onChange={(event) => patchPresentation({ maxLength: Number(event.target.value) })}
            />
          </label>
          {Object.keys(presentationProposal).length > 0 && (
            <div className="button-row presentation-decision-actions">
              <button className="button secondary" type="button" onClick={() => decidePresentation('accepted')}>
                接受提议
              </button>
              <button className="button secondary" type="button" onClick={() => decidePresentation('modified')}>
                修改后接受
              </button>
              <button className="button ghost" type="button" onClick={() => decidePresentation('rejected')}>
                拒绝并关闭
              </button>
            </div>
          )}
        </div>
        {fragments.map((fragment, index) => (
          <div className="node-row" key={`${String(fragment.name)}-${index}`}>
            <label>
              <span>Fragment 名称</span>
              <input
                value={String(fragment.name ?? '')}
                onChange={(event) => {
                  const next = [...fragments]
                  next[index] = { ...fragment, name: event.target.value }
                  patchMetadata(capability, next)
                }}
              />
            </label>
            <label>
              <span>闭包节点（逗号分隔）</span>
              <input
                value={Array.isArray(fragment.nodeIds) ? fragment.nodeIds.join(', ') : ''}
                onChange={(event) => {
                  const next = [...fragments]
                  next[index] = {
                    ...fragment,
                    nodeIds: event.target.value
                      .split(',')
                      .map((item) => item.trim())
                      .filter(Boolean),
                  }
                  patchMetadata(capability, next)
                }}
              />
            </label>
            <small>输入、输出 Schema 与 mapping 已结构化冻结；高级字段可在 JSON 视图调整。</small>
          </div>
        ))}
      </div>
    </section>
  )
}

function NodeInspector({
  node,
  variables,
  agentTargets,
  mappingField,
  onMappingFieldChange,
  onPatch,
  onInsertMapping,
}: {
  node: NodeDefinition
  variables: string[]
  agentTargets: string[]
  mappingField: string
  onMappingFieldChange: (value: string) => void
  onPatch: (patch: Partial<NodeDefinition>) => void
  onInsertMapping: (expression: string) => void
}) {
  const configText = JSON.stringify(node.config ?? {}, null, 2)
  const [localConfig, setLocalConfig] = useState(configText)
  const [configError, setConfigError] = useState('')

  // node 切换时同步本地 config 文本。
  useEffect(() => {
    setLocalConfig(JSON.stringify(node.config ?? {}, null, 2))
    setConfigError('')
  }, [node.id, node.config])

  function commitConfig() {
    try {
      const parsed = JSON.parse(localConfig) as Record<string, unknown>
      onPatch({ config: parsed })
      setConfigError('')
    } catch {
      setConfigError('config JSON 语法错误')
    }
  }

  return (
    <div className="inspector-body">
      <label className="inspector-field">
        <span>名称</span>
        <input
          value={node.displayName}
          onChange={(event) => onPatch({ displayName: event.target.value })}
        />
      </label>
      <div className="inspector-field">
        <span>类型</span>
        <code>{node.type}</code>
      </div>
      <label className="inspector-field checkbox">
        <input
          type="checkbox"
          checked={node.sideEffect ?? false}
          onChange={(event) => onPatch({ sideEffect: event.target.checked })}
        />
        <span>有副作用（sideEffect）</span>
      </label>

      <FallbackEditor node={node} agentTargets={agentTargets} onPatch={onPatch} />

      <div className="inspector-field">
        <span>输入映射</span>
        <div className="mapping-row">
          <input
            aria-label="输入字段名"
            placeholder="字段名，如 name"
            value={mappingField}
            onChange={(event) => onMappingFieldChange(event.target.value)}
          />
        </div>
        <div className="current-mapping">
          {Object.entries(node.input ?? {}).map(([key, value]) => (
            <code key={key} className="variable-chip">
              {key}: {String(value)}
            </code>
          ))}
        </div>
      </div>

      <div className="inspector-field">
        <span>可达上游变量（点击写入当前字段）</span>
        <div className="variable-list">
          {variables.length === 0 ? (
            <span className="inspector-empty">无上游可引用输出</span>
          ) : (
            variables.map((variable) => (
              <button
                key={variable}
                type="button"
                className="variable-chip clickable"
                onClick={() => onInsertMapping(variable)}
                title="写入所选输入字段"
              >
                {variable}
              </button>
            ))
          )}
        </div>
      </div>

      <label className="inspector-field">
        <span>config（JSON）</span>
        <textarea
          className="config-editor"
          spellCheck={false}
          value={localConfig}
          onChange={(event) => setLocalConfig(event.target.value)}
          onBlur={commitConfig}
        />
        {configError && <small className="error-code">{configError}</small>}
      </label>
    </div>
  )
}

// 只允许自动进入恢复的错误码（安全/契约/未知类默认不自动恢复）。
const FALLBACK_ERROR_CODES = ['DEPENDENCY_UNAVAILABLE', 'TIMEOUT', 'PAGE_DRIFT', 'MCP_CONTRACT_DRIFT']

function FallbackEditor({
  node,
  agentTargets,
  onPatch,
}: {
  node: NodeDefinition
  agentTargets: string[]
  onPatch: (patch: Partial<NodeDefinition>) => void
}) {
  const fallback = node.fallback ?? null
  // input/output/agent 节点本身不配置 fallback：只对可能失败的执行节点开放。
  const eligible = !['input', 'output', 'agent'].includes(node.type)

  function toggleCode(code: string, checked: boolean) {
    const current = new Set(fallback?.on ?? [])
    if (checked) current.add(code)
    else current.delete(code)
    const on = [...current]
    if (on.length === 0) {
      onPatch({ fallback: null })
      return
    }
    onPatch({
      fallback: {
        on,
        target: fallback?.target ?? agentTargets[0] ?? '',
        requireUserApproval: fallback?.requireUserApproval ?? false,
      },
    })
  }

  if (!eligible) return null

  return (
    <div className="inspector-field">
      <span>显式失败接力（fallback）</span>
      {agentTargets.length === 0 ? (
        <small className="inspector-empty">需先在图中添加 agent 节点作为恢复目标</small>
      ) : (
        <>
          <div className="fallback-codes">
            {FALLBACK_ERROR_CODES.map((code) => (
              <label className="fallback-code" key={code}>
                <input
                  type="checkbox"
                  checked={(fallback?.on ?? []).includes(code)}
                  onChange={(event) => toggleCode(code, event.target.checked)}
                />
                <span>{code}</span>
              </label>
            ))}
          </div>
          {fallback && (
            <>
              <label className="inspector-field">
                <span>恢复目标（agent 节点）</span>
                <select
                  aria-label="fallback 目标"
                  value={fallback.target}
                  onChange={(event) =>
                    onPatch({ fallback: { ...fallback, target: event.target.value } })
                  }
                >
                  {agentTargets.map((target) => (
                    <option key={target} value={target}>
                      {target}
                    </option>
                  ))}
                </select>
              </label>
              <label className="inspector-field checkbox">
                <input
                  type="checkbox"
                  checked={fallback.requireUserApproval ?? false}
                  onChange={(event) =>
                    onPatch({
                      fallback: { ...fallback, requireUserApproval: event.target.checked },
                    })
                  }
                />
                <span>恢复前需人工确认（requireUserApproval）</span>
              </label>
            </>
          )}
        </>
      )}
    </div>
  )
}
