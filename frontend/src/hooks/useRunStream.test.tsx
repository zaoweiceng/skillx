import { renderHook, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { api, type RunEvent } from '@/api/client'
import { useRunEvents, useRunStream } from './useRunStream'

afterEach(() => { vi.restoreAllMocks(); useRunEvents.setState({ events: {} }) })

it('reconciles terminal events that arrive after the status response', async () => {
  const first = { seq: 1, type: 'run.created', payload: {} } as RunEvent
  const last = { seq: 2, type: 'run.succeeded', payload: {} } as RunEvent
  useRunEvents.getState().addMany('run', [{ ...first, runId: 'run' }])
  const listing = vi.spyOn(api, 'listRunEvents').mockResolvedValue([last])
  const onEvent = vi.fn()
  renderHook(() => useRunStream('run', 'succeeded', true, 0, onEvent))
  await waitFor(() => expect(useRunEvents.getState().events.run.map(item => item.seq)).toEqual([1, 2]))
  expect(listing).toHaveBeenCalledWith('run', 1, 200)
})
