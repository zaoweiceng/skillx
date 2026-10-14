// Terminal styling belongs to the terminal, not the visible message text.
// Only remove complete control sequences; leave literal escape examples untouched.
const escape = String.fromCharCode(27)
const bell = String.fromCharCode(7)
const csi = String.fromCharCode(155)
const osc = new RegExp(`${escape}\\][\\s\\S]*?(?:${bell}|${escape}\\\\)`, 'g')
const controlSequence = new RegExp(`(?:${escape}\\[|${csi})[0-?]*[ -/]*[@-~]`, 'g')

export function terminalText(text: string): string {
  return text.replace(osc, '').replace(controlSequence, '')
}
