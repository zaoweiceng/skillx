import Form from '@rjsf/antd'
import type { FormProps } from '@rjsf/core'
import type { IconButtonProps } from '@rjsf/utils'
import { Button } from 'antd'
import { Plus, Trash2 } from 'lucide-react'

function AddItemButton({ id, disabled, onClick, className }: IconButtonProps) {
  return <Button id={id} className={className} disabled={disabled} onClick={onClick}
    htmlType="button" type="dashed" icon={<Plus size={14} />}>添加一项</Button>
}

function RemoveItemButton({ id, disabled, onClick, className }: IconButtonProps) {
  return <Button id={id} className={className} disabled={disabled} onClick={onClick}
    htmlType="button" aria-label="删除此项" icon={<Trash2 size={14} />}>删除</Button>
}

// The official Ant Design theme handles widgets, validation and schema layout.
// Its automatic column mode keeps nested objects, arrays and textareas full-width.
export default function SchemaForm(props: FormProps) {
  return <Form {...props}
    className={`schema-form ${props.className ?? ''}`}
    formContext={{ colSpan: null, rowGutter: 20, labelAlign: 'left', colon: false, ...props.formContext }}
    templates={{ ...props.templates, ButtonTemplates: {
      AddButton: AddItemButton, RemoveButton: RemoveItemButton, ...props.templates?.ButtonTemplates,
    } }}
  />
}
