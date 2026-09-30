import { describe, expect, it } from 'vitest'

import { rjsfSchema } from '@/lib/utils'

describe('rjsfSchema', () => {
  it('为开放对象启用属性编辑，保留封闭对象与原始契约', () => {
    const schema = {type: 'object', properties: {
      observedPage: {type: 'object', title: '观察页面'},
      closed: {type: 'object', additionalProperties: false},
      rows: {type: 'array', items: {type: 'object'}},
    }}
    expect(rjsfSchema(schema)).toMatchObject({properties: {
      observedPage: {additionalProperties: true},
      closed: {additionalProperties: false},
      rows: {items: {additionalProperties: true}},
    }})
    expect(schema.properties.observedPage).not.toHaveProperty('additionalProperties')
  })
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
