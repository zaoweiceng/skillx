import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

export function rjsfSchema(schema: Record<string, unknown>): Record<string, unknown> {
  const formSchema = { ...schema }
  delete formSchema.$schema
  // JSON Schema permits extra properties by default. RJSF only shows its
  // property editor when this is explicit; otherwise open objects look empty.
  if (formSchema.type === 'object' && !formSchema.properties && !formSchema.$ref &&
      !formSchema.oneOf && !formSchema.anyOf && !formSchema.allOf &&
      formSchema.additionalProperties === undefined) {
    formSchema.additionalProperties = true
  }
  for (const key of ['properties', '$defs', 'definitions', 'patternProperties']) {
    const entries = formSchema[key]
    if (entries && typeof entries === 'object' && !Array.isArray(entries)) {
      formSchema[key] = Object.fromEntries(Object.entries(entries).map(([name, value]) =>
        [name, value && typeof value === 'object' ? rjsfSchema(value) : value],
      ))
    }
  }
  for (const key of ['items', 'additionalProperties']) {
    const value = formSchema[key]
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      formSchema[key] = rjsfSchema(value as Record<string, unknown>)
    }
  }
  for (const key of ['allOf', 'anyOf', 'oneOf', 'prefixItems']) {
    const value = formSchema[key]
    if (Array.isArray(value)) formSchema[key] = value.map(item =>
      item && typeof item === 'object' ? rjsfSchema(item) : item,
    )
  }
  return formSchema
}
