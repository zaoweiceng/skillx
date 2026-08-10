import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const css = readFileSync(`${process.cwd()}/src/index.css`, 'utf8')
const lightTheme = css.match(/:root\[data-theme="light"\]\s*\{(?<body>[\s\S]*?)\n\}/)?.groups
  ?.body

function token(name: string) {
  const value = lightTheme?.match(new RegExp(`--${name}:\\s*(#[0-9a-fA-F]{6})`))?.[1]
  if (!value) throw new Error(`缺少亮色主题 token: ${name}`)
  return value
}

function luminance(hex: string) {
  const channels = [1, 3, 5].map((offset) => Number.parseInt(hex.slice(offset, offset + 2), 16))
  const linear = channels.map((channel) => {
    const value = channel / 255
    return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4
  })
  return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]
}

function contrast(first: string, second: string) {
  const [light, dark] = [luminance(first), luminance(second)].sort((a, b) => b - a)
  return (light + 0.05) / (dark + 0.05)
}

describe('亮色主题语义状态对比度', () => {
  it.each([
    ['notice-text', 'notice-bg'],
    ['error-text', 'error-bg'],
    ['status-neutral', 'panel'],
    ['status-success', 'panel'],
    ['status-active', 'panel'],
    ['status-warning', 'panel'],
    ['status-danger', 'panel'],
  ])('%s 在 %s 上满足普通文字 4.5:1', (foreground, background) => {
    expect(contrast(token(foreground), token(background))).toBeGreaterThanOrEqual(4.5)
  })
})
