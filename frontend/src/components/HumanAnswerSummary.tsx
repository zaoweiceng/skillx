import type { ReactNode } from 'react'
import styles from './HumanInteractionCard.module.css'

type ObjectValue = Record<string, unknown>
type Option = { value?: unknown; label?: string }
const object = (value: unknown): ObjectValue => value && typeof value === 'object' && !Array.isArray(value) ? value as ObjectValue : {}
const fieldNames: Record<string, string> = { approved: '授权决定', note: '补充说明', channel: '选择渠道' }

function valueLabel(value: unknown, schema: ObjectValue, ui: ObjectValue, options: Option[]): ReactNode {
  const variants = [...(Array.isArray(schema.oneOf) ? schema.oneOf : []), ...(Array.isArray(schema.anyOf) ? schema.anyOf : [])]
  const variant = variants.map(object).find(item => item.const === value || (Array.isArray(item.enum) && item.enum.length === 1 && item.enum[0] === value))
  if (typeof variant?.title === 'string') return variant.title
  const index = Array.isArray(schema.enum) ? schema.enum.indexOf(value) : -1
  const names = ui['ui:enumNames'] ?? schema.enumNames
  if (index >= 0 && Array.isArray(names) && typeof names[index] === 'string') return names[index]
  const option = options.find(item => item.value === value)
  if (option?.label) return option.label
  if (Array.isArray(value)) return value.length ? <ul className={styles.values}>{value.map((item, index) => <li key={index}>{valueLabel(item, object(schema.items), object(ui.items), options)}</li>)}</ul> : '未选择'
  if (value && typeof value === 'object') return <HumanAnswerSummary answer={object(value)} schema={schema} ui={ui} options={options} />
  if (typeof value === 'boolean') return value ? '是' : '否'
  return value == null || value === '' ? '未填写' : String(value)
}

export default function HumanAnswerSummary({ answer, schema, ui = {}, options = [] }: {
  answer: ObjectValue; schema: ObjectValue; ui?: ObjectValue; options?: Option[]
}) {
  const properties = object(schema.properties)
  return <dl className={styles.answer}>{Object.entries(answer).map(([key, value]) => {
    const field = object(properties[key])
    const fieldUi = object(ui[key])
    const title = fieldUi['ui:title'] ?? field.title ?? fieldNames[key] ?? key
    return <div className={styles.row} key={key}>
      <dt>{String(title)}</dt>
      <dd>{key === 'approved' && typeof value === 'boolean' ? (value ? '已批准本次操作' : '已拒绝本次操作') : valueLabel(value, field, fieldUi, options)}</dd>
    </div>
  })}</dl>
}
