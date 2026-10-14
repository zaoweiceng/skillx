import { useState, type ReactNode } from 'react'
import { terminalText } from '@/lib/terminalText'
import type { Message } from './useConversation'
import { TaskTrace } from './TaskTrace'
import styles from './TaskTraceTurns.module.css'

export function TaskTraceTurns({ messages, open, renderHeader }: { messages: Message[]; open: boolean; renderHeader?: (selector: ReactNode) => ReactNode }) {
  const [selected, setSelected] = useState<string>()
  let round = 0
  let prompt = ''
  const turns: Array<{ id: string; label: string }> = []
  const seen = new Set<string>()
  for (const message of messages) {
    if (message.role === 'user') {
      round += 1
      prompt = terminalText(message.content.text ?? '').replace(/\s+/g, ' ').trim()
    }
    const id = message.content.taskId
    if (id && !seen.has(id)) {
      seen.add(id)
      const preview = Array.from(prompt)
      const summary = preview.slice(0, 32).join('') + (preview.length > 32 ? '…' : '')
      turns.push({ id, label: `第 ${round || 1} 轮 · ${summary || '任务执行'}` })
    }
  }
  const latest = turns.at(-1)?.id
  const active = turns.find(turn => turn.id === selected)?.id ?? latest
  if (!active) return null
  const selector = <label className={styles.selector}>
      <select aria-label="执行轨迹轮次" value={active}
        onChange={event => setSelected(event.target.value === latest ? undefined : event.target.value)}>
        {[...turns].reverse().map(turn => <option key={turn.id} value={turn.id}>
          {turn.label}{turn.id === latest ? '（最新）' : ''}
        </option>)}
      </select>
    </label>
  return <>
    {renderHeader ? renderHeader(selector) : selector}
    {open && <TaskTrace key={active} id={active} />}
  </>
}
