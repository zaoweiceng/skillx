import { afterEach, expect, it } from 'vitest'
import { readSelectionPreference, saveSelectionPreference } from './selectionPreference'
afterEach(() => localStorage.clear())
it('remembers the latest explicit selection including reasoning for new conversations', () => {
  saveSelectionPreference({agentProfileId: null, traexModel: 'model-a', reasoningEffort: 'high'})
  expect(readSelectionPreference()).toEqual({agentProfileId: null, traexModel: null})
  saveSelectionPreference({agentProfileId: 'traex', traexModel: 'model-b', reasoningEffort: 'low'})
  expect(readSelectionPreference()).toEqual({agentProfileId: 'traex', traexModel: 'model-b', reasoningEffort: 'low'})
})
it('falls back safely when storage is invalid', () => {
  localStorage.setItem('skillx-last-runtime-selection', '{broken')
  expect(readSelectionPreference()).toEqual({agentProfileId: null, traexModel: null})
})
