import { Bot, Database, KeyRound, PanelsTopLeft, ServerCog } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { api } from '@/api/client'

type Health = Awaited<ReturnType<typeof api.getHealth>>

function tone(status: string) {
  if (['available', 'configured', 'reference_only'].includes(status)) return 'success'
  if (['unknown', 'fixture'].includes(status)) return 'warning'
  return 'neutral'
}

export default function SettingsPage() {
  const [health, setHealth] = useState<Health | null>(null)
  const [error, setError] = useState('')
  const [probing, setProbing] = useState(false)

  const refreshHealth = useCallback(() => {
    return api
      .getHealth()
      .then(setHealth)
      .catch((reason: Error) => setError(reason.message))
  }, [])

  useEffect(() => {
    void refreshHealth()
  }, [refreshHealth])

  async function runProbe() {
    setProbing(true)
    setError('')
    try {
      await api.probeProvider()
      await refreshHealth()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Provider 探测失败')
    } finally {
      setProbing(false)
    }
  }

  const provider = health?.selectionProvider
  const agentRuntime = health?.agentRuntime ?? provider
  const llmRuntime = health?.llmRuntime
  const runtimeCards = health
    ? [
        {
          icon: Database,
          name: 'SQLite',
          ...health.runtimes.database,
        },
        {
          icon: ServerCog,
          name: '独立 Worker',
          ...health.runtimes.worker,
        },
        {
          icon: Bot,
          name: 'Agent Runtime',
          status:
            agentRuntime?.type === 'real' && agentRuntime.modelAvailable
              ? 'available'
              : agentRuntime?.type === 'fixture'
                ? 'fixture'
                : 'unconfigured',
          detail: agentRuntime
            ? `${agentRuntime.lastProbe.summary}${
                typeof agentRuntime.lastProbe.totalTokens === 'number'
                  ? ` · operational probe ${agentRuntime.lastProbe.totalTokens} Token`
                  : ''
              }`
            : '未检测',
        },
        {
          icon: ServerCog,
          name: 'LLM Runtime',
          status: llmRuntime?.configured ? 'available' : 'unconfigured',
          detail:
            llmRuntime?.probe.summary ??
            'Direct LLM Runtime 未配置；符合策略时使用无 Tool 的 Agent fallback。',
        },
        {
          icon: PanelsTopLeft,
          name: '浏览器运行时',
          ...health.runtimes.browser,
        },
        {
          icon: KeyRound,
          name: 'Secret Provider',
          ...health.runtimes.secretProvider,
        },
      ]
    : []

  return (
    <div className="page">
      <header className="page-header">
        <div>
          <span className="eyebrow">RUNTIME GOVERNANCE</span>
          <h1>系统与执行环境</h1>
          <p>
            结构化生成优先使用 LLM Runtime；仅启动前未配置时使用受约束的 Agent fallback。
            401、429、超时或畸形输出不会切换 Runtime。
          </p>
        </div>
      </header>
      {error && <div className="error-panel">运行状态探测失败：{error}</div>}
      {!health && !error && <div className="notice-panel">正在探测真实运行状态…</div>}
      {health && (
        <div className="notice-panel">
          当前工作流选择 Provider：<strong>{health.workflowSelectionMode}</strong>
          {health.workflowSelectionMode === 'unavailable' && '。真实语义 Agent 未配置。'}
          {health.workflowSelectionMode === 'fixture' && '。当前仅可验证协议，不代表真实语义能力。'}
          <span>
            当前结构化生成路径：
            <strong>
              {llmRuntime?.configured
                ? ' LLM Runtime'
                : agentRuntime?.configured
                  ? ' Agent fallback'
                  : ' unavailable'}
            </strong>
            。
          </span>
          {provider?.type === 'real' && (
            <>
              <span>
                主动探测会产生真实模型费用，并走 sidecar、function calling 与 Host ToolResult
                同一路径。
              </span>
              <button
                className="button ghost"
                type="button"
                disabled={probing || health.selectionProvider.nextProbeAllowedAt !== null}
                onClick={runProbe}
              >
                {probing ? '探测中…' : '重新探测'}
              </button>
            </>
          )}
        </div>
      )}
      <section className="settings-grid">
        {runtimeCards.map(({ icon: Icon, name, detail, status }) => (
          <article className="setting-card" key={name}>
            <span className="setting-icon">
              <Icon size={19} />
            </span>
            <div>
              <h2>{name}</h2>
              <p>{detail}</p>
            </div>
            <span className={`status-badge ${tone(status)}`}>{status}</span>
          </article>
        ))}
      </section>
    </div>
  )
}
