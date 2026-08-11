import { useEffect } from 'react'
import { create } from 'zustand'

import { eventStreamUrl, type RunEvent } from '@/api/client'

type StoredRunEvent = RunEvent & {
  runId: string
}

type EventState = {
  events: Record<string, StoredRunEvent[]>
  add: (runId: string, event: StoredRunEvent) => void
  addMany: (runId: string, events: StoredRunEvent[]) => void
  clear: (runId: string) => void
}

export const useRunEvents = create<EventState>((set) => ({
  events: {},
  add: (runId, event) =>
    set((state) => {
      const current = state.events[runId] ?? []
      if (current.some((item) => item.seq === event.seq)) return state
      return {
        events: {
          ...state.events,
          [runId]: [...current, event].sort((left, right) => left.seq - right.seq),
        },
      }
    }),
  addMany: (runId, incoming) =>
    set((state) => {
      const bySequence = new Map(
        (state.events[runId] ?? []).map((event) => [event.seq, event]),
      )
      incoming.forEach((event) => bySequence.set(event.seq, event))
      return {
        events: {
          ...state.events,
          [runId]: [...bySequence.values()].sort(
            (left, right) => left.seq - right.seq,
          ),
        },
      }
    }),
  clear: (runId) =>
    set((state) => {
      const events = { ...state.events }
      delete events[runId]
      return { events }
    }),
}))

const TERMINAL_STATUSES = new Set(['succeeded', 'failed', 'cancelled'])
const TERMINAL_EVENTS = new Set([
  'run.failed',
  'run.cancelled',
  'presentation.completed',
  'presentation.failed',
])

export function useRunStream(
  runId: string | undefined,
  runStatus: string | undefined,
  historyLoaded: boolean,
  after: number,
  onEvent: () => void,
): void {
  const add = useRunEvents((state) => state.add)

  useEffect(() => {
    if (
      !runId ||
      !runStatus ||
      !historyLoaded ||
      TERMINAL_STATUSES.has(runStatus)
    ) return
    const stream = new EventSource(eventStreamUrl(runId, after))
    let closed = false
    const eventType = 'run.event'
    const listener = (message: MessageEvent<string>) => {
      const data = JSON.parse(message.data) as StoredRunEvent
      add(runId, { ...data, runId })
      onEvent()
      if (TERMINAL_EVENTS.has(data.type)) close()
    }
    const close = () => {
      if (closed) return
      closed = true
      stream.removeEventListener(eventType, listener as EventListener)
      stream.close()
    }
    stream.addEventListener(eventType, listener as EventListener)
    return close
  }, [add, after, historyLoaded, onEvent, runId, runStatus])
}
