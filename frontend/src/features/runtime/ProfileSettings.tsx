import { useEffect, useState } from 'react'
import { type Profile, runtimeRequest } from './api'
import styles from './runtime.module.css'

const empty = { name: '', kind: 'agent' as Profile['kind'], active: true, isDefault: false,
  config: { adapter: 'traex' as 'traex' | 'pi', modelProfileId: '', baseUrl: '', model: '', apiKeyEnv: 'SKILLX_LLM_API_KEY', structuredMode: 'json_schema', timeoutSeconds: 60 } }
export function ProfileSettings() {
  const [items, setItems] = useState<Profile[]>([])
  const [draft, setDraft] = useState(empty)
  const [editing, setEditing] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [probes, setProbes] = useState<Record<string, string>>({})
  const refresh = () => runtimeRequest<Profile[]>().then(setItems)
  useEffect(() => { refresh().catch(() => setError('执行配置加载失败')) }, [])
  async function save() {
    setBusy(true); setError('')
    const config = draft.kind === 'agent'
      ? { adapter: draft.config.adapter, ...(draft.config.adapter === 'pi' ? { modelProfileId: draft.config.modelProfileId } : {}) }
      : { baseUrl: draft.config.baseUrl, model: draft.config.model, apiKeyEnv: draft.config.apiKeyEnv, structuredMode: draft.config.structuredMode, timeoutSeconds: draft.config.timeoutSeconds }
    try {
      await runtimeRequest(editing ? `/${editing}` : '', editing ? 'PUT' : 'POST', { ...draft, config })
      await refresh(); setDraft(empty); setEditing('')
    } catch (reason) { setError(reason instanceof Error ? reason.message : '保存失败') }
    finally { setBusy(false) }
  }
  async function probe(id: string) {
    setProbes(current => ({ ...current, [id]: '探测中…' }))
    try {
      const result = await runtimeRequest<{ status: string; summary: string }>(`/${id}/probe`, 'POST')
      setProbes(current => ({ ...current, [id]: result.summary }))
    } catch { setProbes(current => ({ ...current, [id]: '探测失败' })) }
  }
  const field = (name: keyof typeof draft.config, label: string) => <label>{label}<input
    value={draft.config[name]} onChange={event => setDraft({ ...draft, config: { ...draft.config, [name]: event.target.value } })} /></label>
  return <section className={styles.panel}>
    <h2>模型服务与 Agent</h2>
    <p>模型服务供直接调用和 Pi 使用；TraeX 使用本机登录及模型配置。</p>
    {error && <p role="alert">{error}</p>}
    <div className={styles.list}>{items.map(item => <div key={item.id}>
      <span>{item.name} · {item.kind === 'model' ? item.config.model : item.config.adapter} {item.isDefault ? '· 默认' : ''} {!item.active ? '· 已停用' : ''}</span>
      <span role="status">{probes[item.id] ?? item.probe?.summary ?? '未探测'}</span>
      <button type="button" disabled={!item.active || probes[item.id] === '探测中…'} onClick={() => void probe(item.id)}>测试连接</button>
      <button type="button" onClick={() => { setEditing(item.id); setDraft({ ...empty, ...item, config: { ...empty.config, ...item.config } }) }}>编辑</button>
    </div>)}</div>
    <form onSubmit={event => { event.preventDefault(); void save() }} className={styles.form}>
      <h3>{editing ? '编辑配置' : '添加配置'}</h3>
      <label>类型<select value={draft.kind} disabled={!!editing} onChange={event => setDraft({ ...draft, kind: event.target.value as Profile['kind'] })}>
        <option value="agent">Agent 执行体</option><option value="model">模型服务</option>
      </select></label>
      <label>名称<input required value={draft.name} onChange={event => setDraft({ ...draft, name: event.target.value })} /></label>
      {draft.kind === 'agent' ? <>
        <label>执行体<select value={draft.config.adapter} onChange={event => setDraft({ ...draft, config: { ...draft.config, adapter: event.target.value as 'pi' | 'traex' } })}>
          <option value="traex">TraeX</option><option value="pi">Pi</option>
        </select></label>
        {draft.config.adapter === 'pi' && <label>模型服务<select required value={draft.config.modelProfileId} onChange={event => setDraft({ ...draft, config: { ...draft.config, modelProfileId: event.target.value } })}>
          <option value="">请选择模型服务</option>{items.filter(item => item.kind === 'model' && item.active).map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
        </select></label>}
      </> : <>{field('baseUrl', 'Base URL')}{field('model', '模型名称')}{field('apiKeyEnv', '密钥环境变量名称')}
        <label>超时（秒）<input type="number" min={1} max={600} value={draft.config.timeoutSeconds}
          onChange={event => setDraft({ ...draft, config: { ...draft.config, timeoutSeconds: Number(event.target.value) } })} /></label>
        <label>结构化输出<select value={draft.config.structuredMode} onChange={event => setDraft({ ...draft, config: { ...draft.config, structuredMode: event.target.value } })}>
          <option value="json_schema">JSON Schema</option><option value="json_object">JSON Object</option><option value="prompt">提示词约束</option>
        </select></label></>}
      <label><input type="checkbox" checked={draft.isDefault} onChange={event => setDraft({ ...draft, isDefault: event.target.checked })} />设为默认</label>
      <label><input type="checkbox" checked={draft.active} onChange={event => setDraft({ ...draft, active: event.target.checked })} />启用</label>
      <button className="button primary" disabled={busy}>保存</button>
      {editing && <button type="button" className="button" onClick={() => { setEditing(''); setDraft(empty) }}>取消编辑</button>}
    </form>
  </section>
}
