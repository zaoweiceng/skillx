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

// 解析组件里的真实颜色声明：既可能是 var(--token)，也可能是写死的十六进制。
// 这样才能发现“固定背景色 + 主题前景变量”的混用，而不是只校验 Token 对。
function resolveColor(expression: string) {
  const trimmed = expression.trim()
  const varMatch = trimmed.match(/^var\(--([a-z-]+)\)$/)
  if (varMatch) return token(varMatch[1])
  const hexMatch = trimmed.match(/^#[0-9a-fA-F]{6}$/)
  if (hexMatch) return trimmed
  throw new Error(`无法解析颜色表达式: ${expression}`)
}

// 取 .trace-drawer 基础规则（行首、非 @media 内），读出最终背景色。
function traceDrawerBackground() {
  const body = css.match(/\n\.trace-drawer\s*\{(?<body>[^}]*)\}/)?.groups?.body
  if (!body) throw new Error('缺少 .trace-drawer 基础规则')
  const value = body.match(/background:\s*([^;]+);/)?.[1]
  if (!value) throw new Error('.trace-drawer 缺少 background 声明')
  return resolveColor(value)
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

describe('亮色主题执行轨迹抽屉最终组合可读', () => {
  // 抽屉内文字继承全局前景变量：标题/状态用 --text、空态/工具/摘要用 --muted、eyebrow/类型用 --accent、
  // 错误码用 --danger。校验它们与抽屉真实背景的组合，而不是抽象 Token 对，才能发现固定色混用。
  it.each([
    ['text'],
    ['muted'],
    ['accent'],
    ['danger'],
  ])('%s 前景在 .trace-drawer 背景上满足普通文字 4.5:1', (foreground) => {
    expect(contrast(token(foreground), traceDrawerBackground())).toBeGreaterThanOrEqual(4.5)
  })
})
