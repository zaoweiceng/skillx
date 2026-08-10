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
          name: '语义 Agent',
          status:
            provider?.type === 'real' && provider.modelAvailable
              ? 'available'
              : provider?.type === 'fixture'
                ? 'fixture'
                : 'unconfigured',
          detail: provider
            ? `${provider.lastProbe.summary}${
                typeof provider.lastProbe.totalTokens === 'number'
                  ? ` · operational probe ${provider.lastProbe.totalTokens} Token`
                  : ''
              }`
            : '未检测',
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
          <p>外部 Runtime 未配置时明确失败，不允许隐式升级到 Agent。</p>
        </div>
      </header>
      {error && <div className="error-panel">运行状态探测失败：{error}</div>}
      {!health && !error && <div className="notice-panel">正在探测真实运行状态…</div>}
      {health && (
        <div className="notice-panel">
          当前工作流选择 Provider：<strong>{health.workflowSelectionMode}</strong>
          {health.workflowSelectionMode === 'unavailable' && '。真实语义 Agent 未配置。'}
          {health.workflowSelectionMode === 'fixture' && '。当前仅可验证协议，不代表真实语义能力。'}
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
