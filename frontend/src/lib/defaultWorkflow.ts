import type { WorkflowDefinition } from '@/api/client'

export const defaultWorkflow: WorkflowDefinition = {
  apiVersion: 'workflow.platform/v1',
  kind: 'Workflow',
  metadata: {
    name: 'greeting',
    capability: {
      summary: '根据姓名生成问候语',
      useWhen: ['需要向某人问候'],
      doNotUseWhen: ['需要发送消息或执行其他副作用'],
      examples: ['问候 Alice'],
      tags: ['问候'],
    },
    presentationPolicy: {
      enabled: true,
      audience: 'end_user',
      producer: 'template',
      template: '{message}',
      language: 'zh-CN',
      maxLength: 500,
    },
  },
  spec: {
    inputSchema: {
      $schema: 'https://json-schema.org/draft/2020-12/schema',
      type: 'object',
      required: ['name'],
      properties: {
        name: { type: 'string', title: '姓名', minLength: 1 },
      },
      additionalProperties: false,
    },
    uiSchema: {},
    outputSchema: {
      $schema: 'https://json-schema.org/draft/2020-12/schema',
      type: 'object',
      required: ['message'],
      properties: { message: { type: 'string' } },
      additionalProperties: false,
    },
    nodes: [
      { id: 'input', type: 'input', displayName: '输入' },
      {
        id: 'format',
        type: 'transform',
        displayName: '生成问候',
        input: { name: '${input.name}' },
        config: { operation: 'template', template: '你好，{name}' },
      },
      {
        id: 'output',
        type: 'output',
        displayName: '输出',
        input: { message: '${format.output.text}' },
      },
    ],
    edges: [
      { source: 'input', target: 'format' },
      { source: 'format', target: 'output' },
    ],
  },
}
