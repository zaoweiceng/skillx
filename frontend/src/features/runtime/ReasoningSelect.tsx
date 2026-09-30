import { Select } from 'antd'
import { Brain } from 'lucide-react'
import styles from './picker.module.css'

export type ReasoningEffort = 'low' | 'medium' | 'high'
export function ReasoningSelect({ value, onChange, disabled }: {
  value?: ReasoningEffort | null; onChange: (value: ReasoningEffort | null) => void; disabled?: boolean
}) {
  return <Select aria-label="推理强度" size="small" variant="borderless" className={styles.trigger}
    classNames={{ popup: { root: styles.popup } }} popupMatchSelectWidth={240}
    prefix={<Brain size={14} />} value={value ?? ''} disabled={disabled}
    options={[
      { value: '', label: '默认强度', description: '使用模型默认设置' },
      { value: 'low', label: '低 · Low', description: '更快响应，适合简单任务' },
      { value: 'medium', label: '中 · Medium', description: '兼顾速度与思考深度' },
      { value: 'high', label: '高 · High', description: '更深入思考，可能需要更长时间' },
    ]} optionRender={option => <div className={styles.option}><span>{option.label}</span><small>{option.data.description}</small></div>}
    onChange={next => onChange(next as ReasoningEffort || null)} />
}
