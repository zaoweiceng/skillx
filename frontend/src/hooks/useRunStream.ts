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
const TERMINAL_EVENTS = new Set(['run.completed', 'run.failed', 'run.cancelled'])

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
      'node.cancelled',
      'node.skipped',
      'node.retry_scheduled',
      'node.stale_result_ignored',
      'node.takeover_ready',
      'node.unknown_outcome',
      'approval.requested',
      'approval.resolved',
      'presentation.started',
      'presentation.delta',
      'presentation.completed',
      'presentation.failed',
      'recovery.attempt_started',
      'recovery.question',
      'recovery.user_answered',
      'run.cancellation_requested',
      'run.recovery_approval_requested',
      'run.recovery_rejected',
      'run.recovery_requested',
      'run.timeout_requested',
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
        const data = JSON.parse(message.data) as Omit<StoredRunEvent, 'type'>
        add(runId, { ...data, type })
        onEvent()
        if (TERMINAL_EVENTS.has(type)) close()
      }
      stream.addEventListener(type, listener as EventListener)
      return { type, listener }
    })
    return close
  }, [add, after, historyLoaded, onEvent, runId, runStatus])
}
