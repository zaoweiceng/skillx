import { request } from '@/api/http'

import type { components } from '@/api/schema'
type Config = components['schemas']['ProfileConfig']
export type Profile = Omit<components['schemas']['ProfileInput'], 'config'> & {
  id: string; probe?: {status: string; summary: string} | null; config: { [K in keyof Config]: Exclude<Config[K], null> }
}
export async function runtimeRequest<T>(path = '', method = 'GET', body?: unknown, signal?: AbortSignal): Promise<T> {
  return request<T>(`/runtime-profiles${path}`, {
    method, signal, ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  })
}
