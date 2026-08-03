import { Blocks, Braces, Cable, LockKeyhole } from 'lucide-react'
import { useEffect, useState } from 'react'

import { api } from '@/api/client'

type Capability = Awaited<ReturnType<typeof api.listCapabilities>>[number]

export default function CapabilitiesPage() {
  const [capabilities, setCapabilities] = useState<Capability[]>([])
  const [error, setError] = useState('')

  useEffect(() => {
    api.listCapabilities().then(setCapabilities).catch((reason: Error) => setError(reason.message))
  }, [])

  return (
    <div className="page">
      <header className="page-header">
        <div>
          <span className="eyebrow">CAPABILITY REGISTRY</span>
          <h1>能力清单</h1>
          <p>统一管理 SkillX、MCP Tool、确定性算子和受限 Agent 入口。</p>
        </div>
      </header>
      {error && <div className="error-panel">{error}</div>}
      <section className="registry-summary">
        <div>
          <Blocks size={19} />
          <strong>{capabilities.length}</strong>
          <span>已注册能力</span>
        </div>
        <div>
          <LockKeyhole size={19} />
          <strong>DENY</strong>
          <span>默认权限策略</span>
        </div>
        <div>
          <Cable size={19} />
          <strong>LOCKED</strong>
          <span>发布版本绑定</span>
        </div>
      </section>
      <section className="panel capability-panel">
        <div className="panel-heading">
          <div>
            <span className="eyebrow">REGISTERED VERSIONS</span>
            <h2>能力版本</h2>
          </div>
        </div>
        {capabilities.length === 0 ? (
          <div className="table-empty">
            <Braces size={22} />
            <p>暂无能力。可通过 API 注册符合 `skillx.dev/v1alpha1` 的可信包。</p>
          </div>
        ) : (
          <div className="capability-table">
            {capabilities.map((item) => (
              <div className="capability-row" key={item.id}>
                <span className="component-icon">
                  <Braces size={16} />
                </span>
                <div>
                  <strong>{item.name}</strong>
                  <small>{item.id}</small>
                </div>
                <span className="mode-tag">{item.type}</span>
                <span>{item.visibility}</span>
                <code>{item.currentVersionId?.slice(0, 8) ?? 'unbound'}</code>
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  )
}
