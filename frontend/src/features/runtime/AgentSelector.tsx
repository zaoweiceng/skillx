import { Alert, Select } from 'antd'
import { useEffect, useState } from 'react'
import { type Profile, runtimeRequest } from './api'
import { ReasoningSelect, type ReasoningEffort } from './ReasoningSelect'
import styles from './picker.module.css'
import { readSelectionPreference, saveSelectionPreference, initializeSelectionPreference } from './selectionPreference'
import { TraexModelSelect } from './TraexModelSelect'

export type Selection = { agentProfileId: string | null; traexModel: string | null; reasoningEffort?: ReasoningEffort | null }
export function AgentSelector({ conversationId, onBusyChange, draftSelection, onDraftChange }: { conversationId: string; draftSelection?: Selection; onDraftChange?: (selection: Selection) => void; onBusyChange?: (busy: boolean) => void }) {
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
  const piModel = profiles.find(item => item.id === active?.config.modelProfileId)?.config.model ?? ''
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
    {active?.config.adapter === 'pi' && /^(gpt-[56]|o[34])/i.test(piModel) && <ReasoningSelect
      value={currentSelection.reasoningEffort} disabled={busy || !loaded}
      onChange={value => void select({ ...currentSelection, reasoningEffort: value })} />}
    <span className="selection-note">下次执行生效</span>
    {error && <Alert type="error" title={error} showIcon />}
  </div>
}
