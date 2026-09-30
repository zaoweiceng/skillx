import { Check, Copy } from 'lucide-react'
import { useState } from 'react'
import styles from './LLMTrace.module.css'
export function TraceCodeBlock({ text, label = 'JSON' }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false)
  const [error, setError] = useState('')
  async function copy() {
    try { await navigator.clipboard.writeText(text); setCopied(true); setError('') }
    catch { setError('复制失败，请手动选择内容') }
  }
  return <div className={styles.codeBlock}>
    <div className={styles.codeToolbar}><span>{label}</span><button type="button" aria-label={`复制${label}`} title={error || (copied ? '已复制' : '复制内容')} onClick={() => void copy()}>
      {copied ? <Check size={12} /> : <Copy size={12} />}<span>{copied ? '已复制' : '复制'}</span></button></div>
    {error && <p className={styles.copyError} role="status">{error}</p>}
    <pre tabIndex={0}>{text}</pre>
  </div>
}
