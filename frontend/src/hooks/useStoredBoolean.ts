import { useState } from 'react'

export function useStoredBoolean(key: string) {
  const [value, setValue] = useState(() => {
    try { return localStorage.getItem(key) === 'true' } catch { return false }
  })
  function update(next: boolean) {
    setValue(next)
    try { localStorage.setItem(key, String(next)) } catch { /* State still works without storage. */ }
  }
  return [value, update] as const
}
