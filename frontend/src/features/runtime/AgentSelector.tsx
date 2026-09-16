import { Alert, Select } from 'antd'
import { useEffect, useState } from 'react'
import { type Profile, runtimeRequest } from './api'
import { TraexModelSelect } from './TraexModelSelect'

type Selection = { agentProfileId: string | null; traexModel: string | null }
export function AgentSelector({ conversationId, onBusyChange }: { conversationId: string; onBusyChange?: (busy: boolean) => void }) {
  const [profiles, setProfiles] = useState<Profile[]>([])
  const [selection, setSelection] = useState<Selection>({ agentProfileId: null, traexModel: null })
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [loaded, setLoaded] = useState(false)
  useEffect(() => { onBusyChange?.(busy) }, [busy, onBusyChange])
  useEffect(() => () => onBusyChange?.(false), [onBusyChange])
  useEffect(() => {
    if (!conversationId) return
    const controller = new AbortController()
    setLoaded(false)
    Promise.all([
      runtimeRequest<Profile[]>('', 'GET', undefined, controller.signal),
      runtimeRequest<Selection>(`/conversations/${conversationId}`, 'GET', undefined, controller.signal),
    ]).then(([items, selected]) => {
      if (!controller.signal.aborted) { setProfiles(items); setSelection(selected); setError(''); setLoaded(true) }
    }).catch(() => { if (!controller.signal.aborted) setError('执行体配置加载失败') })
    return () => controller.abort()
  }, [conversationId])
  async function select(next: Selection) {
    setBusy(true)
    try {
      const saved = await runtimeRequest<Selection>(`/conversations/${conversationId}`, 'PUT', next)
      setSelection(saved); setError('')
    } catch (reason) { setError(reason instanceof Error ? reason.message : '切换失败') }
    finally { setBusy(false) }
  }
  const active = profiles.find(item => selection.agentProfileId ? item.id === selection.agentProfileId : item.kind === 'agent' && item.isDefault)
  return <div className="agent-picker">
    <Select aria-label="Agent 执行体" value={selection.agentProfileId ?? ''} disabled={busy || !loaded}
      style={{ minWidth: 140 }} options={[{ value: '', label: '默认 Agent' }, ...profiles.filter(item => item.kind === 'agent').map(item => ({ value: item.id, label: item.name, disabled: !item.active }))]}
      onChange={value => void select({ agentProfileId: value || null, traexModel: null })} />
    {active?.config.adapter === 'traex' && <TraexModelSelect value={selection.traexModel} disabled={busy || !loaded}
      defaultLabel={active.config.model ? `配置默认 · ${active.config.model}` : 'TraeX 默认模型'}
      onChange={value => void select({ ...selection, traexModel: value })} />}
    <span className="selection-note">下次执行生效</span>
    {error && <Alert type="error" title={error} showIcon />}
  </div>
}
