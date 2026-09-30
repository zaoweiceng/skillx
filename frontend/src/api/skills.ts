import { request } from './http'

export type Skill = {
  id: string; name: string; displayName: string; description: string; enabled: boolean
  revision: number; mode: 'skill' | 'skillx'; skillxVersionId: string | null
  skillxVersion: string | null; interfaceKind: 'direct' | 'agent' | 'llm' | null; updatedAt: string
}
export type SkillInput = Pick<Skill, 'name' | 'displayName' | 'description' | 'enabled'> & {content: string}
export type SkillConversion = {
  kind: 'direct' | 'agent' | 'existing'; inputSchema: Record<string, unknown>
  outputSchema: Record<string, unknown>; source: string
  examples: Array<{input: Record<string, unknown>; expected: Record<string, unknown>}>
}
export type SkillDetail = Skill & {content: string; resources: string[]; conversion: SkillConversion & {hasManifest: boolean}}
export const skillsApi = {
  preview: (content: string) => request<{name: string; displayName: string; description: string}>('/skills/preview', {method: 'POST', body: JSON.stringify({content})}),
  list: () => request<Skill[]>('/skills'),
  get: (id: string) => request<SkillDetail>(`/skills/${id}`),
  create: (input: SkillInput) => request<SkillDetail>('/skills', {method: 'POST', body: JSON.stringify(input)}),
  update: (id: string, input: SkillInput) => request<SkillDetail>(`/skills/${id}`, {method: 'PUT', body: JSON.stringify(input)}),
  import: (path: string) => request<SkillDetail>('/skills/import', {method: 'POST', body: JSON.stringify({path})}),
  remove: (id: string) => request<void>(`/skills/${id}`, {method: 'DELETE'}),
  convert: (id: string, input: SkillConversion & {expectedRevision: number}) => request<SkillDetail>(`/skills/${id}/convert`, {method: 'POST', body: JSON.stringify(input)}),
}
