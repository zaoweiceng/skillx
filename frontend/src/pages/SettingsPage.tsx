import { Bot, Database, KeyRound, PanelsTopLeft, ServerCog } from 'lucide-react'

const settings = [
  {
    icon: Database,
    name: 'SQLite WAL',
    description: '运行、节点、审批与事件的唯一事实来源',
    status: '已启用',
    tone: 'success',
  },
  {
    icon: ServerCog,
    name: '独立 Worker',
    description: '数据库租约、心跳、重试与崩溃恢复',
    status: '需启动进程',
    tone: 'warning',
  },
  {
    icon: Bot,
    name: 'LLM / TraeX',
    description: '仅允许显式 LLM 或 Agent 节点调用',
    status: '未配置',
    tone: 'neutral',
  },
  {
    icon: PanelsTopLeft,
    name: '浏览器运行时',
    description: '页面契约和漂移检测可用，提交 Adapter 待配置',
    status: '部分可用',
    tone: 'warning',
  },
  {
    icon: KeyRound,
    name: 'Secret Provider',
    description: '仅存储引用，禁止明文进入工作流和日志',
    status: '未配置',
    tone: 'neutral',
  },
]

export default function SettingsPage() {
  return (
    <div className="page">
      <header className="page-header">
        <div>
          <span className="eyebrow">RUNTIME GOVERNANCE</span>
          <h1>系统与执行环境</h1>
          <p>外部 Runtime 未配置时明确失败，不允许隐式升级到 Agent。</p>
        </div>
      </header>
      <section className="settings-grid">
        {settings.map(({ icon: Icon, name, description, status, tone }) => (
          <article className="setting-card" key={name}>
            <span className="setting-icon">
              <Icon size={19} />
            </span>
            <div>
              <h2>{name}</h2>
              <p>{description}</p>
            </div>
            <span className={`status-badge ${tone}`}>{status}</span>
          </article>
        ))}
      </section>
    </div>
  )
}
