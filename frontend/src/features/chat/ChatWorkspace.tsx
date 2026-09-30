import { ConfigProvider, Splitter } from 'antd'
import { useEffect, useState, type ReactNode } from 'react'
import styles from './chat.module.css'

const STORAGE_KEY = 'skillx-chat-history-width'
const DEFAULT_WIDTH = 240
const MIN_WIDTH = 180
const MAX_WIDTH = 400
const COMPACT_QUERY = '(max-width: 1200px)'

function clampWidth(width: number) {
  return Number.isFinite(width) ? Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, width)) : DEFAULT_WIDTH
}

function savedWidth() {
  try {
    const saved = localStorage.getItem(STORAGE_KEY)
    return saved === null ? DEFAULT_WIDTH : clampWidth(Number(saved))
  } catch {
    return DEFAULT_WIDTH
  }
}

export function ChatWorkspace({ sidebar, children, collapsed = false, trace, traceOpen = false }: { sidebar: ReactNode; children: ReactNode; collapsed?: boolean; trace?: ReactNode; traceOpen?: boolean }) {
  const [width, setWidth] = useState(savedWidth)
  const [traceWidth, setTraceWidth] = useState(() => {
    try { return Math.max(240, Math.min(640, Number(localStorage.getItem('skillx-chat-trace-width')) || 320)) }
    catch { return 320 }
  })
  const [compact, setCompact] = useState(() => window.matchMedia(COMPACT_QUERY).matches)

  useEffect(() => {
    const query = window.matchMedia(COMPACT_QUERY)
    const update = () => setCompact(query.matches)
    query.addEventListener('change', update)
    update()
    return () => query.removeEventListener('change', update)
  }, [])

  function persistWidth(next: number) {
    const value = clampWidth(next)
    setWidth(value)
    try {
      localStorage.setItem(STORAGE_KEY, String(value))
    } catch {
      // Resizing remains available when browser storage is disabled.
    }
  }

  return <div className="chat-layout">
    <ConfigProvider theme={{components: { Splitter: {splitBarSize: 1, splitTriggerSize: 12} }}}>
    <Splitter className={styles.workspaceSplitter}
      onResize={sizes => { if (traceOpen) setTraceWidth(sizes[1]) }}
      onResizeEnd={sizes => { if (traceOpen) { try { localStorage.setItem('skillx-chat-trace-width', String(sizes[1])) } catch { /* Storage is optional. */ } } }}
      onDraggerDoubleClick={() => { setTraceWidth(320); try { localStorage.setItem('skillx-chat-trace-width', '320') } catch { /* Storage is optional. */ } }}>
    <Splitter.Panel min={280} className={styles.conversationPanel}>
    {compact ? <>{!collapsed && sidebar}{children}</> : <ConfigProvider theme={{components: {
      Splitter: {splitBarSize: 1, splitTriggerSize: 12},
    }}}>
      <Splitter className={styles.workspaceSplitter}
        onResize={sizes => { if (!collapsed) setWidth(clampWidth(sizes[0])) }}
        onResizeEnd={sizes => { if (!collapsed) persistWidth(sizes[0]) }}
        onDraggerDoubleClick={() => persistWidth(DEFAULT_WIDTH)}>
        <Splitter.Panel size={collapsed ? 0 : width} min={collapsed ? 0 : MIN_WIDTH} max={MAX_WIDTH} resizable={!collapsed} className={`${styles.historyPanel} ${collapsed ? styles.historyCollapsed : ''}`}>
          {sidebar}
        </Splitter.Panel>
        <Splitter.Panel min={280} className={styles.messagePanel}>
          {children}
        </Splitter.Panel>
      </Splitter>
    </ConfigProvider>}
    </Splitter.Panel>
    <Splitter.Panel size={traceOpen ? traceWidth : 0} min={traceOpen ? 240 : 0} max="60%" resizable={traceOpen} className={styles.tracePanel}>
      {trace}
    </Splitter.Panel>
    </Splitter>
    </ConfigProvider>
  </div>
}
