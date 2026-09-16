import { Button, Select, Tooltip } from 'antd'
import { RefreshCw } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { runtimeRequest } from './api'

export function TraexModelSelect({ value, onChange, disabled = false, defaultLabel = 'TraeX 默认模型' }: {
  value?: string | null; onChange: (value: string | null) => void; disabled?: boolean; defaultLabel?: string
}) {
  const [models, setModels] = useState<{ id: string; name: string; contextWindow?: number }[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const refresh = useCallback(async (signal?: AbortSignal) => {
    setLoading(true); setError('')
    try {
      const result = await runtimeRequest<typeof models>('/traex/models', 'GET', undefined, signal)
      if (!signal?.aborted) setModels(result)
    } catch (reason) {
      if (!signal?.aborted) setError(reason instanceof Error ? reason.message : '模型加载失败')
    } finally { if (!signal?.aborted) setLoading(false) }
  }, [])
  useEffect(() => {
    const controller = new AbortController()
    void refresh(controller.signal)
    return () => controller.abort()
  }, [refresh])
  const options = [{ value: '', label: defaultLabel }, ...models.map(model => ({ value: model.id, label: model.name }))]
  if (value && !models.some(model => model.id === value)) options.push({ value, label: `${value}（已选择）` })
  return <div className="model-picker">
    <div className="model-picker-row">
      <Select aria-label="TraeX 模型" value={value ?? ''} loading={loading} disabled={disabled}
        showSearch optionFilterProp="label" options={options} onChange={value => onChange(value || null)}
        popupMatchSelectWidth={280} style={{ minWidth: 170, maxWidth: '100%' }} />
      <Tooltip title="刷新模型列表"><Button type="text" aria-label="刷新 TraeX 模型" size="small"
        disabled={loading || disabled} icon={<RefreshCw size={13} />} onClick={() => void refresh()} /></Tooltip>
    </div>
    {error && <small role="alert" className="model-picker-error">{error}</small>}
  </div>
}
