import { useEffect, useState } from 'react'
import type { NodeDefinition } from '@/api/client'
import { type Profile, runtimeRequest } from './api'

export function NodeProfileSelector({ node, onPatch }: {
  node: NodeDefinition; onPatch: (patch: Partial<NodeDefinition>) => void
}) {
  const [profiles, setProfiles] = useState<Profile[]>([])
  const [error, setError] = useState('')
  useEffect(() => {
    let active = true
    runtimeRequest<Profile[]>().then(items => { if (active) setProfiles(items) })
      .catch(() => { if (active) setError('配置加载失败') })
    return () => { active = false }
  }, [])
  if (!['llm', 'agent'].includes(node.type)) return null
  const key = node.type === 'llm' ? 'modelProfileId' : 'agentProfileId'
  return <label className="inspector-field">
    <span>{node.type === 'llm' ? '模型服务' : 'Agent 执行体'}</span>
    <select value={String(node.config?.[key] ?? '')} onChange={event => {
      const config = { ...node.config }
      if (event.target.value) config[key] = event.target.value
      else delete config[key]
      onPatch({ config })
    }}>
      <option value="">发布时使用默认配置</option>
      {profiles.filter(item => item.kind === (node.type === 'llm' ? 'model' : 'agent')).map(item =>
        <option key={item.id} value={item.id} disabled={!item.active}>{item.name}</option>)}
    </select>
    {error && <span role="status">{error}</span>}
  </label>
}
