import type { NodeDefinition } from '@/api/client'

// 只允许自动进入恢复的错误码（安全/契约/未知类默认不自动恢复）。
const FALLBACK_ERROR_CODES = ['DEPENDENCY_UNAVAILABLE', 'TIMEOUT', 'PAGE_DRIFT', 'MCP_CONTRACT_DRIFT']

export function FallbackEditor({
  node,
  agentTargets,
  onPatch,
}: {
  node: NodeDefinition
  agentTargets: string[]
  onPatch: (patch: Partial<NodeDefinition>) => void
}) {
  const fallback = node.fallback ?? null
  // input/output/agent 节点本身不配置 fallback：只对可能失败的执行节点开放。
  const eligible = !['input', 'output', 'agent'].includes(node.type)

  function toggleCode(code: string, checked: boolean) {
    const current = new Set(fallback?.on ?? [])
    if (checked) current.add(code)
    else current.delete(code)
    const on = [...current]
    if (on.length === 0) {
      onPatch({ fallback: null })
      return
    }
    onPatch({
      fallback: {
        on,
        target: fallback?.target ?? agentTargets[0] ?? '',
        requireUserApproval: fallback?.requireUserApproval ?? false,
      },
    })
  }

  if (!eligible) return null

  return (
    <div className="inspector-field">
      <span>显式失败接力（fallback）</span>
      {agentTargets.length === 0 ? (
        <small className="inspector-empty">需先在图中添加 agent 节点作为恢复目标</small>
      ) : (
        <>
          <div className="fallback-codes">
            {FALLBACK_ERROR_CODES.map((code) => (
              <label className="fallback-code" key={code}>
                <input
                  type="checkbox"
                  checked={(fallback?.on ?? []).includes(code)}
                  onChange={(event) => toggleCode(code, event.target.checked)}
                />
                <span>{code}</span>
              </label>
            ))}
          </div>
          {fallback && (
            <>
              <label className="inspector-field">
                <span>恢复目标（agent 节点）</span>
                <select
                  aria-label="fallback 目标"
                  value={fallback.target}
                  onChange={(event) =>
                    onPatch({ fallback: { ...fallback, target: event.target.value } })
                  }
                >
                  {agentTargets.map((target) => (
                    <option key={target} value={target}>
                      {target}
                    </option>
                  ))}
                </select>
              </label>
              <label className="inspector-field checkbox">
                <input
                  type="checkbox"
                  checked={fallback.requireUserApproval ?? false}
                  onChange={(event) =>
                    onPatch({
                      fallback: { ...fallback, requireUserApproval: event.target.checked },
                    })
                  }
                />
                <span>恢复前需人工确认（requireUserApproval）</span>
              </label>
            </>
          )}
        </>
      )}
    </div>
  )
}
