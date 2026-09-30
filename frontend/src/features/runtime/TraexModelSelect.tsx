import { Button, Select, Tooltip } from 'antd'
import { RefreshCw } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { ReasoningSelect, type ReasoningEffort } from './ReasoningSelect'
import styles from './picker.module.css'
import { runtimeRequest } from './api'

export function TraexModelSelect({ value, onChange, disabled = false, compact = false, defaultLabel = 'TraeX 默认模型', reasoningEffort, onReasoningChange }: {
  reasoningEffort?: ReasoningEffort | null; onReasoningChange?: (value: ReasoningEffort | null) => void
  compact?: boolean; value?: string | null; onChange: (value: string | null) => void; disabled?: boolean; defaultLabel?: string
}) {
  const [models, setModels] = useState<{ id: string; name: string; contextWindow?: number; reasoningEfforts?: string[] }[]>([])
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
  const selectedModel = models.find(model => model.id === value)
  return <><div className="model-picker">
    <div className="model-picker-row">
      <Select size={compact ? 'small' : 'middle'} variant={compact ? 'borderless' : 'outlined'} className={styles.trigger} classNames={{ popup: { root: styles.popup } }}
        popupRender={menu => <><div className={styles.menuHeading}>选择模型 · 输入名称搜索</div>{menu}</>} aria-label="TraeX 模型" value={value ?? ''} loading={loading} disabled={disabled}
        showSearch optionFilterProp="label" options={options} onChange={value => onChange(value || null)}
        optionRender={option => {
          const model = models.find(item => item.id === option.value)
          return <div className={styles.option}><span>{option.label}</span><small>{model ? [model.contextWindow ? `${Math.round(model.contextWindow / 1000)}K 上下文` : '', model.reasoningEfforts?.length ? '支持推理强度' : ''].filter(Boolean).join(' · ') : '使用执行体配置的模型'}</small></div>
        }}
        popupMatchSelectWidth={300} style={{ width: compact ? 152 : 200, maxWidth: '100%' }} />
      <Tooltip title="刷新模型列表"><Button type="text" aria-label="刷新 TraeX 模型" size="small"
        disabled={loading || disabled} icon={<RefreshCw size={13} />} onClick={() => void refresh()} /></Tooltip>
    </div>
    {error && <small role="alert" className="model-picker-error">{error}</small>}
  </div>
    {onReasoningChange && Boolean(selectedModel?.reasoningEfforts?.length) && <ReasoningSelect value={reasoningEffort} disabled={disabled || loading} onChange={onReasoningChange} />}
  </>
}
