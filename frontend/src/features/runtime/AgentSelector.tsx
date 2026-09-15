import { useEffect, useState } from 'react'
import { type Profile, runtimeRequest } from './api'

export function AgentSelector({ conversationId }: { conversationId: string }) {
  const [profiles, setProfiles] = useState<Profile[]>([])
  const [selected, setSelected] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    if (!conversationId) return
    let active = true
    Promise.all([
      runtimeRequest<Profile[]>(),
      runtimeRequest<{ agentProfileId: string | null }>(`/conversations/${conversationId}`),
    ]).then(([items, selection]) => {
      if (active) { setProfiles(items); setSelected(selection.agentProfileId ?? ''); setError('') }
    }).catch(() => { if (active) setError('执行体配置加载失败') })
    return () => { active = false }
  }, [conversationId])
  async function select(value: string) {
    setBusy(true)
    try {
      await runtimeRequest(`/conversations/${conversationId}`, 'PUT', { agentProfileId: value || null })
      setSelected(value); setError('')
    } catch (reason) { setError(reason instanceof Error ? reason.message : '切换失败') }
    finally { setBusy(false) }
  }
  return <label>
    <span>执行体 · 下次生效 </span>
    <select aria-label="Agent 执行体" value={selected} disabled={busy || !conversationId}
      onChange={event => void select(event.target.value)}>
      <option value="">默认 Agent</option>
      {profiles.filter(item => item.kind === 'agent').map(item =>
        <option key={item.id} value={item.id} disabled={!item.active}>{item.name} · {item.config.adapter}</option>)}
    </select>
    {error && <span role="status">{error}</span>}
  </label>
}
