import { expect, it } from 'vitest'
import { terminalText } from './terminalText'

it('removes terminal colors and cursor controls while preserving log lines', () => {
  const log = '\u001b[38;5;9m\u001b[1m[FAIL]\u001b[0m\n/atlas/test_helper.go:249\n\u001b[2K详情';
  expect(terminalText(log)).toBe('[FAIL]\n/atlas/test_helper.go:249\n详情')
})

it('keeps terminal hyperlink labels without interpreting HTML', () => {
  expect(terminalText('\u001b]8;;https://example.com\u0007查看\u001b]8;;\u001b\\ <script>text</script>'))
    .toBe('查看 <script>text</script>')
})

it('preserves ordinary brackets, literal escape examples and unfinished sequences', () => {
  const text = '[0m example \\x1b[31m\n代码 [1, 2]\n\u001b[38;'
  expect(terminalText(text)).toBe(text)
})
