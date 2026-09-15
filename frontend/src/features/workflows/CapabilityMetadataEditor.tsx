import { Plus } from 'lucide-react'
import type { WorkflowDefinition } from '@/api/client'

export function CapabilityMetadataEditor({
  definition,
  onChange,
  selectedNodeId,
}: {
  definition: WorkflowDefinition
  onChange: (definition: WorkflowDefinition) => void
  selectedNodeId: string
}) {
  const capability =
    typeof definition.metadata.capability === 'object' && definition.metadata.capability
      ? (definition.metadata.capability as Record<string, unknown>)
      : {}
  const fragments = Array.isArray(definition.metadata.callableFragments)
    ? (definition.metadata.callableFragments as Array<Record<string, unknown>>)
    : []
  const presentation =
    typeof definition.metadata.presentationPolicy === 'object' &&
    definition.metadata.presentationPolicy
      ? (definition.metadata.presentationPolicy as Record<string, unknown>)
      : {}
  const presentationProposal =
    typeof definition.metadata.presentationPolicyProposal === 'object' &&
    definition.metadata.presentationPolicyProposal
      ? (definition.metadata.presentationPolicyProposal as Record<string, unknown>)
      : {}
  const presentationDecision =
    typeof definition.metadata.presentationPolicyDecision === 'object' &&
    definition.metadata.presentationPolicyDecision
      ? (definition.metadata.presentationPolicyDecision as Record<string, unknown>)
      : {}
  const presentationDraft =
    Object.keys(presentation).length > 0 ? presentation : presentationProposal

  function policyFrom(source: Record<string, unknown>) {
    const enabled = source.enabled === true
    return {
      enabled,
      audience: enabled ? 'end_user' : 'internal',
      producer: String(source.producer ?? 'template'),
      template: String(source.template ?? '执行结果：{json}'),
      prompt: String(source.prompt ?? ''),
      language: String(source.language ?? 'zh-CN'),
      maxLength: Number(source.maxLength ?? 2000),
    }
  }

  const proposalPolicy = policyFrom(presentationProposal)
  const draftPolicy = policyFrom(presentationDraft)
  const presentationEdited = JSON.stringify(proposalPolicy) !== JSON.stringify(draftPolicy)

  function patchMetadata(nextCapability: Record<string, unknown>, nextFragments = fragments) {
    onChange({
      ...definition,
      metadata: {
        ...definition.metadata,
        capability: nextCapability,
        callableFragments: nextFragments,
      },
    })
  }

  function patchPresentation(patch: Record<string, unknown>) {
    onChange({
      ...definition,
      metadata: {
        ...definition.metadata,
        presentationPolicy: { ...presentationDraft, ...patch },
        presentationPolicyDecision: { status: 'pending' },
      },
    })
  }

  function decidePresentation(status: 'accepted' | 'modified' | 'rejected') {
    const policy =
      status === 'rejected'
        ? {
            enabled: false,
            audience: 'internal',
            producer: 'template',
            template: '执行结果：{json}',
            prompt: String(presentationDraft.prompt ?? ''),
            language: String(presentationDraft.language ?? 'zh-CN'),
            maxLength: Number(presentationDraft.maxLength ?? 2000),
          }
        : status === 'accepted'
          ? proposalPolicy
          : draftPolicy
    onChange({
      ...definition,
      metadata: {
        ...definition.metadata,
        presentationPolicy: policy,
        presentationPolicyDecision: {
          status,
          reviewer: 'local-user',
          reviewedAt: new Date().toISOString(),
        },
      },
    })
  }

  function insertPresentationField(field: string) {
    const current = String(presentationDraft.template ?? '执行结果：{json}')
    patchPresentation({ template: `${current}{${field}}` })
  }

  function listValue(key: string) {
    return Array.isArray(capability[key]) ? (capability[key] as unknown[]).join(', ') : ''
  }

  function patchList(key: string, value: string) {
    patchMetadata({
      ...capability,
      [key]: value
        .split(',')
        .map((item) => item.trim())
        .filter(Boolean),
    })
  }

  function addFragment() {
    if (!selectedNodeId) return
    const sourceProperties = definition.spec.inputSchema.properties as
      | Record<string, unknown>
      | undefined
    const sourceField = Object.keys(sourceProperties ?? {})[0] ?? 'input'
    const next = [
      ...fragments,
      {
        name: `fragment-${fragments.length + 1}`,
        summary: '',
        nodeIds: [selectedNodeId],
        inputSchema: definition.spec.inputSchema,
        outputSchema: {
          type: 'object',
          properties: { result: {} },
          required: ['result'],
        },
        inputMapping: { [sourceField]: `\${input.${sourceField}}` },
        outputMapping: { result: `\${${selectedNodeId}.output}` },
      },
    ]
    patchMetadata(capability, next)
  }

  return (
    <section className="panel capability-metadata-panel">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">CAPABILITY CONTRACT</span>
          <h2>目录元数据与 Fragment 端口</h2>
        </div>
        <button className="button ghost" type="button" onClick={addFragment} disabled={!selectedNodeId}>
          <Plus size={13} /> 将选中节点声明为 Fragment
        </button>
      </div>
      <div className="inspector-fields">
        <label>
          <span>能力摘要</span>
          <input
            value={String(capability.summary ?? '')}
            onChange={(event) => patchMetadata({ ...capability, summary: event.target.value })}
          />
        </label>
        <label>
          <span>适用场景（逗号分隔）</span>
          <input
            value={listValue('useWhen')}
            onChange={(event) => patchList('useWhen', event.target.value)}
          />
        </label>
        <label>
          <span>不适用场景（逗号分隔）</span>
          <input
            value={listValue('doNotUseWhen')}
            onChange={(event) => patchList('doNotUseWhen', event.target.value)}
          />
        </label>
        <label>
          <span>示例（逗号分隔）</span>
          <input
            value={listValue('examples')}
            onChange={(event) => patchList('examples', event.target.value)}
          />
        </label>
        <label>
          <span>标签（逗号分隔）</span>
          <input
            value={listValue('tags')}
            onChange={(event) => patchList('tags', event.target.value)}
          />
        </label>
        <div className="presentation-policy-editor">
          {Object.keys(presentationProposal).length > 0 && (
            <div className="notice-panel presentation-proposal-evidence">
              <strong>固化 Agent 提议</strong>
              <span>
                来源：{String(presentationProposal.source ?? 'unknown')} · 受众：
                {String(presentationProposal.resultAudience ?? 'unknown')} · 消费者：
                {String(presentationProposal.consumer ?? 'unknown')} · 位置：
                {String(presentationProposal.terminalOrFragment ?? 'unknown')} · 置信度：
                {Number(presentationProposal.confidence ?? 0).toFixed(2)}
              </span>
              <span>{String(presentationProposal.reason ?? '')}</span>
              {Array.isArray(presentationProposal.evidence) && (
                <ul>
                  {presentationProposal.evidence.map((item) => (
                    <li key={String(item)}>{String(item)}</li>
                  ))}
                </ul>
              )}
              <span>审阅状态：{String(presentationDecision.status ?? 'pending')}</span>
            </div>
          )}
          <label>
            <input
              type="checkbox"
              checked={presentationDraft.enabled === true}
              onChange={(event) =>
                patchPresentation({
                  enabled: event.target.checked,
                  audience: event.target.checked ? 'end_user' : 'internal',
                })
              }
            />
            <span>面向最终用户生成阅读结果</span>
          </label>
          <label>
            <span>生成方式</span>
            <select
              value={String(presentationDraft.producer ?? 'template')}
              onChange={(event) => patchPresentation({ producer: event.target.value })}
            >
              <option value="template">稳定模板（零 LLM）</option>
              <option value="llm">LLM 整理</option>
            </select>
          </label>
          {String(presentationDraft.producer ?? 'template') === 'template' && (
            <>
              <label>
                <span>模板（支持 {'{json}'} 和顶层字段）</span>
                <input
                  value={String(presentationDraft.template ?? '执行结果：{json}')}
                  onChange={(event) => patchPresentation({ template: event.target.value })}
                />
              </label>
              <div className="presentation-field-chips" aria-label="可插入输出字段">
                {['json', ...Object.keys(
                  (definition.spec.outputSchema.properties as Record<string, unknown>) ?? {},
                )].map((field) => (
                  <button
                    className="metric-chip"
                    type="button"
                    key={field}
                    onClick={() => insertPresentationField(field)}
                  >
                    {`{${field}}`}
                  </button>
                ))}
              </div>
            </>
          )}
          {String(presentationDraft.producer ?? 'template') === 'llm' && (
            <label>
              <span>LLM Prompt</span>
              <textarea
                value={String(presentationDraft.prompt ?? '')}
                onChange={(event) => patchPresentation({ prompt: event.target.value })}
              />
            </label>
          )}
          <label>
            <span>语言</span>
            <input
              value={String(presentationDraft.language ?? 'zh-CN')}
              onChange={(event) => patchPresentation({ language: event.target.value })}
            />
          </label>
          <label>
            <span>最大长度</span>
            <input
              type="number"
              min={64}
              max={8000}
              value={Number(presentationDraft.maxLength ?? 2000)}
              onChange={(event) => patchPresentation({ maxLength: Number(event.target.value) })}
            />
          </label>
          {Object.keys(presentationProposal).length > 0 && (
            <div className="button-row presentation-decision-actions">
              <button
                className="button secondary"
                type="button"
                disabled={presentationEdited}
                onClick={() => decidePresentation('accepted')}
              >
                接受提议
              </button>
              <button className="button secondary" type="button" onClick={() => decidePresentation('modified')}>
                修改后接受
              </button>
              <button className="button ghost" type="button" onClick={() => decidePresentation('rejected')}>
                拒绝并关闭
              </button>
            </div>
          )}
        </div>
        {fragments.map((fragment, index) => (
          <div className="node-row" key={`${String(fragment.name)}-${index}`}>
            <label>
              <span>Fragment 名称</span>
              <input
                value={String(fragment.name ?? '')}
                onChange={(event) => {
                  const next = [...fragments]
                  next[index] = { ...fragment, name: event.target.value }
                  patchMetadata(capability, next)
                }}
              />
            </label>
            <label>
              <span>闭包节点（逗号分隔）</span>
              <input
                value={Array.isArray(fragment.nodeIds) ? fragment.nodeIds.join(', ') : ''}
                onChange={(event) => {
                  const next = [...fragments]
                  next[index] = {
                    ...fragment,
                    nodeIds: event.target.value
                      .split(',')
                      .map((item) => item.trim())
                      .filter(Boolean),
                  }
                  patchMetadata(capability, next)
                }}
              />
            </label>
            <small>输入、输出 Schema 与 mapping 已结构化冻结；高级字段可在 JSON 视图调整。</small>
          </div>
        ))}
      </div>
    </section>
  )
}

