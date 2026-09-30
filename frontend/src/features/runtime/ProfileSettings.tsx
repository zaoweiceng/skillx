import { Alert, Button, Card, Checkbox, Empty, Input, InputNumber, Modal, Select, Space, Tag } from 'antd'
import { Bot, Plus, ServerCog, Trash2 } from 'lucide-react'
import { useEffect, useState } from 'react'
import { type Profile, runtimeRequest } from './api'
import { TraexModelSelect } from './TraexModelSelect'
import styles from './runtime.module.css'

type Model = { id: string; reasoning: boolean; images: boolean; contextWindow: number; maxOutputTokens: number }
const newModel = (): Model => ({ id: '', reasoning: false, images: false, contextWindow: 128000, maxOutputTokens: 8192 })
const empty = { name: '', kind: 'model' as Profile['kind'], active: true, isDefault: false,
  config: { adapter: 'pi' as 'traex' | 'pi', modelProfileId: '', baseUrl: '', model: '', apiKey: '', apiKeyEnv: '', models: [newModel()], structuredMode: 'json_schema', timeoutSeconds: 60 } }
export function ProfileSettings() {
  const [items, setItems] = useState<Profile[]>([])
  const [draft, setDraft] = useState(empty)
  const [editing, setEditing] = useState('')
  const [open, setOpen] = useState(false)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [probing, setProbing] = useState<string[]>([])
  const [probes, setProbes] = useState<Record<string, string>>({})
  const refresh = () => runtimeRequest<Profile[]>().then(setItems)
  useEffect(() => { refresh().catch(() => setError('执行配置加载失败')) }, [])
  async function save() {
    setBusy(true); setError('')
    const config = draft.kind === 'agent'
      ? { adapter: draft.config.adapter, ...(draft.config.adapter === 'pi' ? { modelProfileId: draft.config.modelProfileId || null, model: draft.config.model || null } : draft.config.model ? { model: draft.config.model } : {}) }
      : { baseUrl: draft.config.baseUrl, model: draft.config.model || draft.config.models[0]?.id,
        models: draft.config.models, ...(draft.config.apiKey ? { apiKey: draft.config.apiKey } : {}),
        ...(draft.config.apiKeyEnv ? { apiKeyEnv: draft.config.apiKeyEnv } : {}),
        structuredMode: draft.config.structuredMode, timeoutSeconds: draft.config.timeoutSeconds }
    try {
      await runtimeRequest(editing ? `/${editing}` : '', editing ? 'PUT' : 'POST', { name: draft.name, kind: draft.kind, active: draft.active, isDefault: draft.isDefault, config })
      await refresh(); setOpen(false); setDraft(empty); setEditing('')
    } catch (reason) { setError(reason instanceof Error ? reason.message : '保存失败') }
    finally { setBusy(false) }
  }
  async function probe(id: string) {
    setProbing(current => [...current, id])
    try {
      const result = await runtimeRequest<{ status: string; summary: string }>(`/${id}/probe`, 'POST')
      setProbes(current => ({ ...current, [id]: result.summary })); await refresh()
    } catch { setProbes(current => ({ ...current, [id]: '探测失败' })) }
    finally { setProbing(current => current.filter(value => value !== id)) }
  }
  const editModel = (index: number, update: Partial<Model>) => setDraft(current => ({ ...current, config: { ...current.config, models: current.config.models.map((model, position) => position === index ? { ...model, ...update } : model),
    model: update.id !== undefined && current.config.model === current.config.models[index].id ? update.id : current.config.model } }))
  const service = items.find(item => item.id === draft.config.modelProfileId)
  return <section className={styles.panel}>
    <div className={styles.heading}><div><h2>模型服务与 Agent</h2><p>Pi Agent 默认执行。添加模型服务后，可以在对话中切换模型。</p></div>
      <Button icon={<Plus size={15} />} type="primary" onClick={() => { setDraft(empty); setEditing(''); setError(''); setOpen(true) }}>添加配置</Button></div>
    {error && !open && <Alert type="error" title={error} showIcon />}
    {(['agent', 'model'] as const).map(kind => <div key={kind} className={styles.group}>
      <div className={styles.groupTitle}>{kind === 'agent' ? 'AGENT 执行体' : '模型服务'}<span>{items.filter(item => item.kind === kind).length}</span></div>
      <div className={styles.cards}>{items.filter(item => item.kind === kind).map(item => <Card key={item.id} size="small" className={styles.card}>
        <div className={styles.cardHeading}><span className={styles.icon}>{kind === 'agent' ? <Bot size={20} /> : <ServerCog size={20} />}</span>
          <div><strong>{item.name}</strong><small>{kind === 'model' ? (item.config.models?.map(model => model.id).join(' · ') || item.config.model) : item.config.adapter === 'traex' ? `TraeX · ${item.config.model || '默认模型'}` : 'Pi Agent · SDK 0.85.1'}</small></div>
          {item.isDefault && <Tag>默认</Tag>}</div>
        <div className={styles.status}><Tag color={!item.active ? 'default' : item.probe?.status === 'available' ? 'success' : item.probe?.status === 'failed' ? 'error' : 'default'}>
          {!item.active ? '已停用' : item.probe?.status === 'available' ? '可用' : item.probe?.status === 'failed' ? '连接失败' : '未探测'}</Tag>
          <span role="status">{probing.includes(item.id) ? '正在测试连接…' : probes[item.id] ?? item.probe?.summary ?? '测试连接以验证当前配置'}</span></div>
        <Space><Button size="small" disabled={!item.active} loading={probing.includes(item.id)} onClick={() => void probe(item.id)}>测试连接</Button>
          <Button size="small" type="text" onClick={() => { setEditing(item.id); setError(''); setDraft({ ...empty, ...item, config: { ...empty.config, ...item.config, apiKey: '', models: item.config.models?.map(model => ({ ...newModel(), ...model })) ?? [{ ...newModel(), id: item.config.model ?? '' }] } }); setOpen(true) }}>编辑</Button></Space>
      </Card>)}</div>
      {!items.some(item => item.kind === kind) && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={kind === 'model' ? '添加 Base URL、API Key 和模型即可开始使用 Pi' : '尚未添加 Agent'} />}
    </div>)}
    <Modal title={editing ? '编辑配置' : '添加配置'} open={open} footer={null} onCancel={() => { if (!busy) { setOpen(false); setDraft(empty) } }} destroyOnHidden width={680}>
      <form onSubmit={event => { event.preventDefault(); void save() }} className={styles.form}>
        {error && <Alert type="error" title={error} showIcon className={styles.full} />}
        <label>类型<Select aria-label="配置类型" value={draft.kind} disabled={!!editing} onChange={kind => setDraft({ ...draft, kind, config: { ...draft.config, model: '' } })}
          options={[{ value: 'model', label: '模型服务' }, { value: 'agent', label: 'Agent 执行体' }]} /></label>
        <label>名称<Input aria-label="配置名称" required value={draft.name} onChange={event => setDraft({ ...draft, name: event.target.value })} placeholder="例如：我的模型服务" /></label>
        {draft.kind === 'agent' ? <>
          <label>执行体<Select aria-label="配置执行体" value={draft.config.adapter} onChange={adapter => setDraft({ ...draft, config: { ...draft.config, adapter, model: '' } })}
            options={[{ value: 'pi', label: 'Pi Agent' }, { value: 'traex', label: 'TraeX（兼容）' }]} /></label>
          {draft.config.adapter === 'pi' ? <>
            <label>模型服务<Select aria-label="模型服务" value={draft.config.modelProfileId || ''}
              onChange={modelProfileId => setDraft({ ...draft, config: { ...draft.config, modelProfileId, model: '' } })}
              options={[{ value: '', label: '默认模型服务' }, ...items.filter(item => item.kind === 'model' && item.active).map(item => ({ value: item.id, label: item.name }))]} /></label>
            <label>默认模型<Select aria-label="Agent 默认模型" value={draft.config.model || ''} onChange={model => setDraft({ ...draft, config: { ...draft.config, model } })}
              options={[{ value: '', label: '使用服务默认模型' }, ...(service?.config.models ?? (service?.config.model ? [{ id: service.config.model }] : [])).map(model => ({ value: model.id, label: model.id }))]} /></label>
          </> : <label>默认模型<TraexModelSelect value={draft.config.model} onChange={model => setDraft({ ...draft, config: { ...draft.config, model: model ?? '' } })} /></label>}
        </> : <>
          <label>Base URL<Input aria-label="Base URL" required placeholder="https://api.example.com/v1" value={draft.config.baseUrl} onChange={event => setDraft({ ...draft, config: { ...draft.config, baseUrl: event.target.value } })} /></label>
          <label>API Key<Input.Password aria-label="API Key" autoComplete="new-password" required={!editing && !draft.config.apiKeyEnv} placeholder={editing ? '留空保留已保存密钥' : '输入 API Key'} value={draft.config.apiKey} onChange={event => setDraft({ ...draft, config: { ...draft.config, apiKey: event.target.value } })} /></label>
          <p className={styles.full}>支持 OpenAI 兼容 Chat Completions 服务。密钥保存到本机，不会回传到页面。</p>
          <div className={styles.full}>{draft.config.models.map((model, index) => <div key={index} className={styles.modelRow}>
            <label>模型 ID<Input required aria-label={`模型 ID ${index + 1}`} value={model.id} placeholder="模型 ID" onChange={event => editModel(index, { id: event.target.value })} /></label>
            <label>上下文 Token<InputNumber aria-label={`上下文 Token ${index + 1}`} min={1024} max={10000000} value={model.contextWindow} onChange={value => editModel(index, { contextWindow: value ?? 128000 })} /></label>
            <label>最大输出 Token<InputNumber aria-label={`最大输出 Token ${index + 1}`} min={1} max={model.contextWindow} value={model.maxOutputTokens} onChange={value => editModel(index, { maxOutputTokens: value ?? 8192 })} /></label>
            <Checkbox checked={model.reasoning} onChange={event => editModel(index, { reasoning: event.target.checked })}>支持推理</Checkbox>
            <Checkbox aria-label={`支持图片 ${index + 1}`} checked={model.images} onChange={event => editModel(index, { images: event.target.checked })}>支持图片</Checkbox>
            <Button aria-label={`删除模型 ${index + 1}`} icon={<Trash2 size={14} />} disabled={draft.config.models.length === 1} onClick={() => setDraft({ ...draft, config: { ...draft.config, models: draft.config.models.filter((_, position) => position !== index), model: draft.config.model === model.id ? '' : draft.config.model } })} />
          </div>)}<Button icon={<Plus size={14} />} onClick={() => setDraft({ ...draft, config: { ...draft.config, models: [...draft.config.models, newModel()] } })}>添加模型</Button></div>
          <label>默认模型<Select aria-label="服务默认模型" value={draft.config.model || draft.config.models[0]?.id || undefined} onChange={model => setDraft({ ...draft, config: { ...draft.config, model } })} options={draft.config.models.filter(model => model.id).map(model => ({ value: model.id, label: model.id }))} /></label>
          <label>超时（秒）<InputNumber min={1} max={600} value={draft.config.timeoutSeconds} style={{ width: '100%' }} onChange={value => setDraft({ ...draft, config: { ...draft.config, timeoutSeconds: value ?? 60 } })} /></label>
          <label>LLM 节点结构化输出<Select value={draft.config.structuredMode} onChange={structuredMode => setDraft({ ...draft, config: { ...draft.config, structuredMode } })} options={[{ value: 'json_schema', label: 'JSON Schema' }, { value: 'json_object', label: 'JSON Object' }, { value: 'prompt', label: '提示词约束' }]} /></label>
        </>}
        <Space className={styles.full}><Checkbox checked={draft.isDefault} onChange={event => setDraft({ ...draft, isDefault: event.target.checked })}>设为默认</Checkbox>
          <Checkbox checked={draft.active} onChange={event => setDraft({ ...draft, active: event.target.checked })}>启用</Checkbox></Space>
        <div className={styles.actions}><Button disabled={busy} onClick={() => { setOpen(false); setDraft(empty) }}>取消</Button><Button htmlType="submit" type="primary" loading={busy}>保存</Button></div>
      </form>
    </Modal>
  </section>
}
