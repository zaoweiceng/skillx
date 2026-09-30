type CapabilityDescription = {
  name?: string
  displayName?: string
  description?: string
  interfaces?: Record<string, {description?: string}>
}

export function capabilityDescription(item: CapabilityDescription): string {
  if (item.description?.trim() && item.description !== item.name && item.description !== item.displayName) return item.description.trim()
  const descriptions = [...Object.values(item.interfaces ?? {}).map(value => value.description)]
    .map(value => value?.trim() ?? '')
    .filter(value => value && value !== item.name && value !== item.displayName)
  return [...new Set(descriptions)].join('；') || '暂无简介'
}

export function workflowDescription(metadata: Record<string, unknown>): string {
  const capability = metadata.capability as {summary?: unknown} | undefined
  const description = capability?.summary || metadata.description
  return typeof description === 'string' && description.trim() ? description.trim() : '暂无简介'
}
