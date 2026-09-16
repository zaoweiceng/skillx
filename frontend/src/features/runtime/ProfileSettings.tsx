import { Alert, Button, Card, Checkbox, Empty, Input, InputNumber, Modal, Select, Space, Tag } from 'antd'
import { Bot, Plus, ServerCog } from 'lucide-react'
import { useEffect, useState } from 'react'
import { type Profile, runtimeRequest } from './api'
import { TraexModelSelect } from './TraexModelSelect'
import styles from './runtime.module.css'

const empty = { name: '', kind: 'agent' as Profile['kind'], active: true, isDefault: false,
  config: { adapter: 'traex' as 'traex' | 'pi', modelProfileId: '', baseUrl: '', model: '', apiKeyEnv: 'SKILLX_LLM_API_KEY', structuredMode: 'json_schema', timeoutSeconds: 60 } }
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
      ? { adapter: draft.config.adapter, ...(draft.config.adapter === 'pi' ? { modelProfileId: draft.config.modelProfileId } : draft.config.model ? { model: draft.config.model } : {}) }
      : { baseUrl: draft.config.baseUrl, model: draft.config.model, apiKeyEnv: draft.config.apiKeyEnv, structuredMode: draft.config.structuredMode, timeoutSeconds: draft.config.timeoutSeconds }
    try {
      await runtimeRequest(editing ? `/${editing}` : '', editing ? 'PUT' : 'POST', { ...draft, config })
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
  const field = (name: 'baseUrl' | 'model' | 'apiKeyEnv', label: string, placeholder: string) => <label>{label}<Input required placeholder={placeholder}
    value={draft.config[name]} onChange={event => setDraft({ ...draft, config: { ...draft.config, [name]: event.target.value } })} /></label>
  return <section className={styles.panel}>
    <div className={styles.heading}><div><h2>模型服务与 Agent</h2><p>为不同任务选择执行体与模型，连接一次，随时切换。</p></div>
      <Button icon={<Plus size={15} />} type="primary" onClick={() => { setDraft(empty); setEditing(''); setError(''); setOpen(true) }}>添加配置</Button></div>
    {error && !open && <Alert type="error" title={error} showIcon />}
    {(['agent', 'model'] as const).map(kind => <div key={kind} className={styles.group}>
      <div className={styles.groupTitle}>{kind === 'agent' ? 'AGENT 执行体' : '模型服务'}<span>{items.filter(item => item.kind === kind).length}</span></div>
      <div className={styles.cards}>{items.filter(item => item.kind === kind).map(item => <Card key={item.id} size="small" className={styles.card}>
        <div className={styles.cardHeading}><span className={styles.icon}>{kind === 'agent' ? <Bot size={20} /> : <ServerCog size={20} />}</span>
          <div><strong>{item.name}</strong><small>{kind === 'model' ? item.config.model : item.config.adapter === 'traex' ? `TraeX · ${item.config.model || '默认模型'}` : 'Pi Agent'}</small></div>
          {item.isDefault && <Tag>默认</Tag>}</div>
        <div className={styles.status}><Tag color={!item.active ? 'default' : item.probe?.status === 'available' ? 'success' : item.probe?.status === 'failed' ? 'error' : 'default'}>
          {!item.active ? '已停用' : item.probe?.status === 'available' ? '可用' : item.probe?.status === 'failed' ? '连接失败' : '未探测'}</Tag>
          <span role="status">{probing.includes(item.id) ? '正在测试连接…' : probes[item.id] ?? item.probe?.summary ?? '测试连接以验证当前配置'}</span></div>
        <Space><Button size="small" disabled={!item.active} loading={probing.includes(item.id)} onClick={() => void probe(item.id)}>测试连接</Button>
          <Button size="small" type="text" onClick={() => { setEditing(item.id); setError(''); setDraft({ ...empty, ...item, config: { ...empty.config, ...item.config } }); setOpen(true) }}>编辑</Button></Space>
      </Card>)}</div>
      {!items.some(item => item.kind === kind) && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={kind === 'model' ? '尚未添加模型服务，Pi 和 LLM 直连可在此配置' : '尚未添加 Agent'} />}
    </div>)}
    <Modal title={editing ? '编辑配置' : '添加配置'} open={open} footer={null} onCancel={() => { if (!busy) setOpen(false) }} destroyOnHidden>
      <form onSubmit={event => { event.preventDefault(); void save() }} className={styles.form}>
        {error && <Alert type="error" title={error} showIcon className={styles.full} />}
        <label>类型<Select aria-label="配置类型" value={draft.kind} disabled={!!editing} onChange={kind => setDraft({ ...draft, kind })}
          options={[{ value: 'agent', label: 'Agent 执行体' }, { value: 'model', label: '模型服务' }]} /></label>
        <label>名称<Input required value={draft.name} onChange={event => setDraft({ ...draft, name: event.target.value })} placeholder="例如：日常任务 Agent" /></label>
        {draft.kind === 'agent' ? <>
          <label>执行体<Select aria-label="配置执行体" value={draft.config.adapter} onChange={adapter => setDraft({ ...draft, config: { ...draft.config, adapter, model: '' } })}
            options={[{ value: 'traex', label: 'TraeX' }, { value: 'pi', label: 'Pi Agent' }]} /></label>
          {draft.config.adapter === 'pi' ? <label>模型服务<Select aria-label="模型服务" value={draft.config.modelProfileId || undefined} placeholder="请选择模型服务"
            onChange={modelProfileId => setDraft({ ...draft, config: { ...draft.config, modelProfileId } })}
            options={items.filter(item => item.kind === 'model' && item.active).map(item => ({ value: item.id, label: item.name }))} /></label>
            : <label>默认模型<TraexModelSelect value={draft.config.model} onChange={model => setDraft({ ...draft, config: { ...draft.config, model: model ?? '' } })} /></label>}
          <p className={styles.full}>{draft.config.adapter === 'traex' ? '使用本机 TraeX 登录。这里设置默认模型，对话中可以单独选择。' : '使用平台模型服务，工具调用由 SkillX 管理。'}</p>
        </> : <>{field('baseUrl', 'Base URL', 'https://api.example.com/v1')}{field('model', '模型名称', '模型 ID')}{field('apiKeyEnv', '密钥环境变量名称', 'SKILLX_LLM_API_KEY')}
          <label>超时（秒）<InputNumber min={1} max={600} value={draft.config.timeoutSeconds} style={{ width: '100%' }}
            onChange={value => setDraft({ ...draft, config: { ...draft.config, timeoutSeconds: value ?? 60 } })} /></label>
          <label>结构化输出<Select value={draft.config.structuredMode} onChange={structuredMode => setDraft({ ...draft, config: { ...draft.config, structuredMode } })}
            options={[{ value: 'json_schema', label: 'JSON Schema' }, { value: 'json_object', label: 'JSON Object' }, { value: 'prompt', label: '提示词约束' }]} /></label></>}
        <Space className={styles.full}><Checkbox checked={draft.isDefault} onChange={event => setDraft({ ...draft, isDefault: event.target.checked })}>设为默认</Checkbox>
          <Checkbox checked={draft.active} onChange={event => setDraft({ ...draft, active: event.target.checked })}>启用</Checkbox></Space>
        <div className={styles.actions}><Button disabled={busy} onClick={() => setOpen(false)}>取消</Button><Button htmlType="submit" type="primary" loading={busy}>保存</Button></div>
      </form>
    </Modal>
  </section>
}
