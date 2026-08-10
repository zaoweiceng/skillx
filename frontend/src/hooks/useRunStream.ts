import { useEffect } from 'react'
import { create } from 'zustand'

import { eventStreamUrl } from '@/api/client'

type RunEvent = {
  runId: string
  seq: number
  type: string
  payload: Record<string, unknown>
}

type EventState = {
  events: Record<string, RunEvent[]>
  add: (runId: string, event: RunEvent) => void
  clear: (runId: string) => void
}

export const useRunEvents = create<EventState>((set) => ({
  events: {},
  add: (runId, event) =>
    set((state) => {
      const current = state.events[runId] ?? []
      if (current.some((item) => item.seq === event.seq)) return state
      return { events: { ...state.events, [runId]: [...current, event] } }
    }),
  clear: (runId) =>
    set((state) => {
      const events = { ...state.events }
      delete events[runId]
      return { events }
    }),
}))

const TERMINAL_STATUSES = new Set(['succeeded', 'failed', 'cancelled'])
const TERMINAL_EVENTS = new Set(['run.completed', 'run.failed', 'run.cancelled'])

export function useRunStream(
  runId: string | undefined,
  runStatus: string | undefined,
  onEvent: () => void,
): void {
  const add = useRunEvents((state) => state.add)

  useEffect(() => {
    if (!runId || !runStatus || TERMINAL_STATUSES.has(runStatus)) return
    const stream = new EventSource(eventStreamUrl(runId))
    const eventTypes = [
      'run.created',
      'run.started',
      'run.completed',
      'run.failed',
      'run.cancelled',
      'node.ready',
      'node.started',
      'node.completed',
      'node.failed',
      'node.skipped',
      'approval.requested',
      'approval.resolved',
    ]
    let closed = false
    let listeners: Array<{ type: string; listener: (message: MessageEvent<string>) => void }> = []
    const close = () => {
      if (closed) return
      closed = true
      listeners.forEach(({ type, listener }) =>
        stream.removeEventListener(type, listener as EventListener),
      )
      stream.close()
    }
    listeners = eventTypes.map((type) => {
      const listener = (message: MessageEvent<string>) => {
        const data = JSON.parse(message.data) as Omit<RunEvent, 'type'>
        add(runId, { ...data, type })
        onEvent()
        if (TERMINAL_EVENTS.has(type)) close()
      }
      stream.addEventListener(type, listener as EventListener)
      return { type, listener }
    })
    return close
  }, [add, onEvent, runId, runStatus])
}
