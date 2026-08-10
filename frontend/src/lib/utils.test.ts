import { describe, expect, it } from 'vitest'

import { rjsfSchema } from '@/lib/utils'

describe('rjsfSchema', () => {
  it('仅移除 RJSF validator 不支持的 Draft 2020-12 元声明', () => {
    const schema = {
      $schema: 'https://json-schema.org/draft/2020-12/schema',
      type: 'object',
      properties: { name: { type: 'string' } },
      required: ['name'],
    }

    expect(rjsfSchema(schema)).toEqual({
      type: 'object',
      properties: { name: { type: 'string' } },
      required: ['name'],
    })
    expect(schema.$schema).toBe('https://json-schema.org/draft/2020-12/schema')
  })
})
