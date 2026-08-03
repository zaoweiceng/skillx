import { ArrowLeft, CheckCircle2, Save, Send, TriangleAlert } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { api, type Workflow, type WorkflowDefinition } from '@/api/client'
import { WorkflowGraph } from '@/components/WorkflowGraph'

type Diagnostic = { level: string; code: string; path: string; message: string }

export default function WorkflowEditorPage() {
  const { id } = useParams()
  const [workflow, setWorkflow] = useState<Workflow>()
  const [source, setSource] = useState('')
  const [diagnostics, setDiagnostics] = useState<Diagnostic[]>([])
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (!id) return
    api
      .getWorkflow(id)
      .then((item) => {
        setWorkflow(item)
        setSource(JSON.stringify(item.definition, null, 2))
      })
      .catch((reason: Error) => setError(reason.message))
  }, [id])

  const preview = useMemo(() => {
    try {
      return JSON.parse(source) as WorkflowDefinition
    } catch {
      return undefined
    }
  }, [source])

  async function save(): Promise<Workflow | undefined> {
    if (!workflow || !id) return
    setBusy(true)
    setError('')
    try {
      const definition = JSON.parse(source) as WorkflowDefinition
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

  async function validate(): Promise<boolean> {
    const saved = await save()
    if (!saved || !id) return false
    const result = await api.validateWorkflow(id)
    setDiagnostics(result.diagnostics ?? [])
    setNotice(result.valid ? `静态检查通过 · ${result.digest}` : '静态检查未通过')
    return result.valid
  }

  async function publish() {
    const valid = await validate()
    if (!id || !workflow) return
    if (!valid) return
    const version = await api.publishWorkflow(id)
    const component = await api.createComponent(
      workflow.name,
      '由工作流编辑器发布的锁定版本组件。',
      version.id,
    )
    setNotice(
      `已发布不可变版本 v${version.version}，组件入口 ${component.id.slice(0, 8)} 已创建`,
    )
  }

  if (!workflow) {
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
          <button className="button ghost" onClick={save} disabled={busy}>
            <Save size={15} /> 保存
          </button>
          <button className="button ghost" onClick={validate} disabled={busy}>
            <CheckCircle2 size={15} /> 静态检查
          </button>
          <button className="button primary" onClick={publish} disabled={busy}>
            <Send size={15} /> 发布版本
          </button>
        </div>
      </header>

      {(error || notice) && (
        <div className={error ? 'error-panel' : 'notice-panel'}>{error || notice}</div>
      )}

      <section className="editor-layout">
        <div className="panel source-panel">
          <div className="panel-heading">
            <div>
              <span className="eyebrow">WORKFLOW DSL</span>
              <h2>定义 JSON</h2>
            </div>
            <span>{source.split('\n').length} 行</span>
          </div>
          <textarea
            aria-label="工作流 JSON 定义"
            className="source-editor"
            spellCheck={false}
            value={source}
            onChange={(event) => setSource(event.target.value)}
          />
        </div>
        <div className="panel preview-panel">
          <div className="panel-heading">
            <div>
              <span className="eyebrow">LIVE PREVIEW</span>
              <h2>DAG 预览</h2>
            </div>
          </div>
          {preview ? (
            <WorkflowGraph definition={preview} compact />
          ) : (
            <div className="graph-error">
              <TriangleAlert size={20} />
              JSON 语法错误，无法生成预览
            </div>
          )}
          <div className="diagnostics">
            {diagnostics.map((item) => (
              <div className={`diagnostic ${item.level}`} key={`${item.code}-${item.path}`}>
                <strong>{item.code}</strong>
                <span>{item.message}</span>
                <small>{item.path}</small>
              </div>
            ))}
          </div>
        </div>
      </section>
    </div>
  )
}
