import type { Selection } from './AgentSelector'
const KEY = 'skillx-last-runtime-selection'
export function readSelectionPreference(): Selection {
  try {
    const saved = JSON.parse(localStorage.getItem(KEY) ?? 'null')
    if (saved && (saved.agentProfileId === null || typeof saved.agentProfileId === 'string') &&
        (saved.traexModel === null || typeof saved.traexModel === 'string')) {
      return { agentProfileId: saved.agentProfileId, traexModel: saved.traexModel,
        reasoningEffort: ['low', 'medium', 'high'].includes(saved.reasoningEffort) ? saved.reasoningEffort : null }
    }
  } catch { /* Storage can be unavailable; conversation settings remain authoritative. */ }
  return { agentProfileId: null, traexModel: null }
}
export function saveSelectionPreference(selection: Selection) {
  try { localStorage.setItem(KEY, JSON.stringify(selection)) } catch { /* Keep current selection usable. */ }
}

export function initializeSelectionPreference(selection: Selection) {
  try {
    // Bootstrap existing installations from the open conversation only once.
    // Subsequent history loads must never overwrite a user's explicit choice.
    if (localStorage.getItem(KEY) === null) saveSelectionPreference(selection)
  } catch { /* Preferences are optional when storage is blocked. */ }
}
