import { Alert, Select } from 'antd'
import { useEffect, useState } from 'react'
import { type Profile, runtimeRequest } from './api'
import { ReasoningSelect, type ReasoningEffort } from './ReasoningSelect'
import styles from './picker.module.css'
import { readSelectionPreference, saveSelectionPreference, initializeSelectionPreference } from './selectionPreference'
import { TraexModelSelect } from './TraexModelSelect'

export type Selection = { agentProfileId: string | null; traexModel: string | null; reasoningEffort?: ReasoningEffort | null; modelProfileId?: string | null; model?: string | null }
export function AgentSelector({ cacheUsage = [], conversationId, onBusyChange, draftSelection, onDraftChange }: { cacheUsage?: Array<{ inputTokens?: number | null; cacheReadTokens?: number | null }>; conversationId: string; draftSelection?: Selection; onDraftChange?: (selection: Selection) => void; onBusyChange?: (busy: boolean) => void }) {
  const [profiles, setProfiles] = useState<Profile[]>([])
  const [selection, setSelection] = useState<Selection>(readSelectionPreference)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [loaded, setLoaded] = useState(false)
  useEffect(() => { onBusyChange?.(busy) }, [busy, onBusyChange])
  useEffect(() => () => onBusyChange?.(false), [onBusyChange])
  useEffect(() => {
    const controller = new AbortController()
    setLoaded(false)
    Promise.all([
      runtimeRequest<Profile[]>('', 'GET', undefined, controller.signal),
      conversationId ? runtimeRequest<Selection>(`/conversations/${conversationId}`, 'GET', undefined, controller.signal) : Promise.resolve(readSelectionPreference()),
    ]).then(([items, selected]) => {
      if (!controller.signal.aborted) { if (conversationId) initializeSelectionPreference(selected); setProfiles(items); setSelection(selected); setError(''); setLoaded(true) }
    }).catch(() => { if (!controller.signal.aborted) setError('执行体配置加载失败') })
    return () => controller.abort()
  }, [conversationId])
  async function select(next: Selection) {
    if (!conversationId) { saveSelectionPreference(next); onDraftChange?.(next); setSelection(next); return }
    setBusy(true)
    try {
      const saved = await runtimeRequest<Selection>(`/conversations/${conversationId}`, 'PUT', next)
      setSelection(saved); saveSelectionPreference(saved); setError('')
    } catch (reason) { setError(reason instanceof Error ? reason.message : '切换失败') }
    finally { setBusy(false) }
  }
  const currentSelection = !conversationId && draftSelection ? draftSelection : selection
  const active = profiles.find(item => currentSelection.agentProfileId ? item.id === currentSelection.agentProfileId : item.kind === 'agent' && item.isDefault)
  const services = profiles.filter(item => item.kind === 'model' && item.active)
  const piService = services.find(item => item.id === (currentSelection.modelProfileId || active?.config.modelProfileId)) ?? services.find(item => item.isDefault)
  const piModel = currentSelection.model || active?.config.model || piService?.config.model || ''
  const supportsReasoning = piService?.config.models?.find(model => model.id === piModel)?.reasoning || /^(gpt-[56]|o[34])/i.test(piModel)
  const measured = cacheUsage.filter(item => item.inputTokens != null && item.cacheReadTokens != null)
  const inputTokens = measured.reduce((sum, item) => sum + (item.inputTokens ?? 0), 0)
  const cachedTokens = measured.reduce((sum, item) => sum + (item.cacheReadTokens ?? 0), 0)
  const cacheLabel = conversationId && inputTokens > 0 ? `缓存命中${(100 * cachedTokens / inputTokens).toFixed(2)}%` : null
  return <div className="agent-picker">
    <Select size="small" variant="borderless" className={styles.trigger} classNames={{ popup: { root: styles.popup } }}
      popupMatchSelectWidth={260} popupRender={menu => <><div className={styles.menuHeading}>选择执行体</div>{menu}</>} aria-label="Agent 执行体" value={currentSelection.agentProfileId ?? ''} disabled={busy || !loaded}
      style={{ width: 128 }} options={[{ value: '', label: '默认 Agent', description: '使用工作空间默认配置' }, ...profiles.filter(item => item.kind === 'agent').map(item => ({ value: item.id, label: item.name, description: item.config.adapter === 'traex' ? 'TraeX · 本地 Agent' : 'Pi · 自定义模型服务', disabled: !item.active }))]}
      optionRender={option => <div className={styles.option}><span>{option.label}</span><small>{option.data.description}</small></div>}
      onChange={value => void select({ agentProfileId: value || null, traexModel: null, reasoningEffort: null })} />
    {active?.config.adapter === 'traex' && <TraexModelSelect compact value={currentSelection.traexModel} disabled={busy || !loaded}
      defaultLabel={active.config.model ? `配置默认 · ${active.config.model}` : 'TraeX 默认模型'}
      reasoningEffort={currentSelection.reasoningEffort}
      onReasoningChange={value => void select({ ...currentSelection, reasoningEffort: value })}
      onChange={value => void select({ ...currentSelection, traexModel: value, reasoningEffort: null })} />}
    {active?.config.adapter === 'pi' && <Select aria-label="Pi 模型" size="small" variant="borderless" disabled={busy || !loaded}
      style={{ minWidth: 160, maxWidth: 280 }} value={currentSelection.modelProfileId && currentSelection.model ? JSON.stringify([currentSelection.modelProfileId, currentSelection.model]) : ''}
      options={[{ value: '', label: piModel ? `默认 · ${piModel}` : '请先添加模型服务' }, ...services.map(service => ({ label: service.name,
        options: (service.config.models ?? (service.config.model ? [{ id: service.config.model }] : [])).map(model => ({ value: JSON.stringify([service.id, model.id]), label: model.id })) }))]}
      onChange={value => { const [modelProfileId, model] = value ? JSON.parse(value) as string[] : [null, null]; void select({ ...currentSelection, modelProfileId, model, reasoningEffort: null }) }} />}
    {active?.config.adapter === 'pi' && supportsReasoning && <ReasoningSelect
      value={currentSelection.reasoningEffort} disabled={busy || !loaded}
      onChange={value => void select({ ...currentSelection, reasoningEffort: value })} />}
    {cacheLabel && <span style={{ fontSize: 11, color: '#999', whiteSpace: 'nowrap' }} title={`本对话已报告的缓存读取 Token ÷ 输入 Token（含缓存）${measured.length < cacheUsage.length ? '；部分调用未报告缓存数据' : ''}`}>{cacheLabel}</span>}
    <span className="selection-note">下次执行生效</span>
    {error && <Alert type="error" title={error} showIcon />}
  </div>
}
