import { useEffect, useState } from 'react'
import type { NodeDefinition } from '@/api/client'
import { NodeProfileSelector } from '@/features/runtime/NodeProfileSelector'
import { FallbackEditor } from './FallbackEditor'

export function NodeInspector({
  node,
  variables,
  agentTargets,
  mappingField,
  onMappingFieldChange,
  onPatch,
  onInsertMapping,
}: {
  node: NodeDefinition
  variables: string[]
  agentTargets: string[]
  mappingField: string
  onMappingFieldChange: (value: string) => void
  onPatch: (patch: Partial<NodeDefinition>) => void
  onInsertMapping: (expression: string) => void
}) {
  const configText = JSON.stringify(node.config ?? {}, null, 2)
  const [localConfig, setLocalConfig] = useState(configText)
  const [configError, setConfigError] = useState('')

  // node 切换时同步本地 config 文本。
  useEffect(() => {
    setLocalConfig(JSON.stringify(node.config ?? {}, null, 2))
    setConfigError('')
  }, [node.id, node.config])

  function commitConfig() {
    try {
      const parsed = JSON.parse(localConfig) as Record<string, unknown>
      onPatch({ config: parsed })
      setConfigError('')
    } catch {
      setConfigError('config JSON 语法错误')
    }
  }

  return (
    <div className="inspector-body">
      <NodeProfileSelector node={node} onPatch={onPatch} />
      <label className="inspector-field">
        <span>名称</span>
        <input
          value={node.displayName}
          onChange={(event) => onPatch({ displayName: event.target.value })}
        />
      </label>
      <div className="inspector-field">
        <span>类型</span>
        {['llm', 'agent'].includes(node.type) ? <select aria-label="AI 执行方式" value={node.type}
          onChange={event => {
            const config = { ...node.config }
            delete config.modelProfile
            delete config.agentProfile
            delete config.modelProfileId
            delete config.agentProfileId
            if (event.target.value === 'agent') {
              config.maxSteps = 6
              config.allowedCapabilities = []
            }
            onPatch({ type: event.target.value, config })
          }}>
          <option value="llm">直接调用 LLM</option><option value="agent">Agent 执行</option>
        </select> : <code>{node.type}</code>}
      </div>
      <label className="inspector-field checkbox">
        <input
          type="checkbox"
          checked={node.sideEffect ?? false}
          onChange={(event) => onPatch({ sideEffect: event.target.checked })}
        />
        <span>有副作用（sideEffect）</span>
      </label>

      <FallbackEditor node={node} agentTargets={agentTargets} onPatch={onPatch} />

      <div className="inspector-field">
        <span>输入映射</span>
        <div className="mapping-row">
          <input
            aria-label="输入字段名"
            placeholder="字段名，如 name"
            value={mappingField}
            onChange={(event) => onMappingFieldChange(event.target.value)}
          />
        </div>
        <div className="current-mapping">
          {Object.entries(node.input ?? {}).map(([key, value]) => (
            <code key={key} className="variable-chip">
              {key}: {String(value)}
            </code>
          ))}
        </div>
      </div>

      <div className="inspector-field">
        <span>可达上游变量（点击写入当前字段）</span>
        <div className="variable-list">
          {variables.length === 0 ? (
            <span className="inspector-empty">无上游可引用输出</span>
          ) : (
            variables.map((variable) => (
              <button
                key={variable}
                type="button"
                className="variable-chip clickable"
                onClick={() => onInsertMapping(variable)}
                title="写入所选输入字段"
              >
                {variable}
              </button>
            ))
          )}
        </div>
      </div>

      <label className="inspector-field">
        <span>config（JSON）</span>
        <textarea
          className="config-editor"
          spellCheck={false}
          value={localConfig}
          onChange={(event) => setLocalConfig(event.target.value)}
          onBlur={commitConfig}
        />
        {configError && <small className="error-code">{configError}</small>}
      </label>
    </div>
  )
}

