import { useEffect, useState } from 'react'
import type { Run } from '@/api/client'
import { request } from '@/api/http'
import { type Profile, runtimeRequest } from './api'
import styles from './runtime.module.css'

export function HandoffPanel({ run, onStarted }: { run: Run; onStarted: () => void }) {
  const [profiles, setProfiles] = useState<Profile[]>([])
  const [profile, setProfile] = useState('')
  const [node, setNode] = useState('')
  const [reason, setReason] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    let active = true
    runtimeRequest<Profile[]>().then(items => { if (active) setProfiles(items) })
      .catch(() => { if (active) setError('执行体配置加载失败') })
    return () => { active = false }
  }, [])
  async function submit() {
    setBusy(true); setError('')
    try {
      await request(`/runs/${run.id}/handoff`, { method: 'POST', body: JSON.stringify({
        nodeRunId: node, agentProfileId: profile, reason,
      }) })
      onStarted()
    } catch (failure) { setError(failure instanceof Error ? failure.message : '启动接力失败') }
    finally { setBusy(false) }
  }
  return <details className={styles.panel}>
    <summary>交给独立 Agent 接力</summary>
    <p>选择失败节点和执行体。本次接力保留已完成步骤，并记录改选原因。</p>
    <form className={styles.form} onSubmit={event => { event.preventDefault(); void submit() }}>
      <label>失败节点<select required value={node} onChange={event => setNode(event.target.value)}>
        <option value="">请选择</option>{run.nodes?.filter(item => item.status === 'failed').map(item =>
          <option key={item.id} value={item.id}>{item.nodeId} · 第 {item.attempt} 次尝试</option>)}
      </select></label>
      <label>恢复 Agent<select required value={profile} onChange={event => setProfile(event.target.value)}>
        <option value="">请选择</option>{profiles.filter(item => item.kind === 'agent' && item.active).map(item =>
          <option key={item.id} value={item.id}>{item.name} · {item.config.adapter}</option>)}
      </select></label>
      <label>接力原因<input required maxLength={1000} value={reason} onChange={event => setReason(event.target.value)} /></label>
      {error && <p role="alert">{error}</p>}
      <button className="button primary" disabled={busy}>{busy ? '启动中…' : '启动独立接力'}</button>
    </form>
  </details>
}
