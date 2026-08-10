import {
  Blocks,
  Bot,
  Boxes,
  Cable,
  ChevronRight,
  CircleDot,
  Moon,
  Settings,
  Sun,
} from 'lucide-react'
import { NavLink, Outlet } from 'react-router-dom'
import { useTheme } from '@/hooks/useTheme'

const navigation = [
  { to: '/chat', label: '对话', icon: Bot },
  { to: '/components', label: '组件', icon: Boxes },
  { to: '/workflows', label: '工作流', icon: Cable },
  { to: '/capabilities', label: '能力', icon: Blocks },
  { to: '/settings', label: '系统', icon: Settings },
]

export default function AppShell() {
  const { isDark, toggleTheme } = useTheme()
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">
            <CircleDot size={17} />
          </span>
          <div>
            <strong>SkillX</strong>
            <small>WORKFLOW CONTROL</small>
          </div>
        </div>
        <nav className="primary-nav" aria-label="主导航">
          {navigation.map(({ to, label, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              className={({ isActive }) => (isActive ? 'nav-item active' : 'nav-item')}
            >
              <Icon size={17} strokeWidth={1.7} />
              <span>{label}</span>
              <ChevronRight className="nav-arrow" size={14} />
            </NavLink>
          ))}
        </nav>
        <div className="runtime-status">
          <span className="pulse-dot" />
          <div>
            <strong>本地运行时</strong>
            <small>真实状态见系统页</small>
          </div>
        </div>
        <button
          className="theme-toggle"
          type="button"
          aria-label={isDark ? '切换到亮色主题' : '切换到暗色主题'}
          aria-pressed={isDark}
          onClick={toggleTheme}
        >
          {isDark ? <Moon size={15} /> : <Sun size={15} />}
          <span>{isDark ? '暗色' : '亮色'}</span>
        </button>
      </aside>
      <main className="workspace">
        <Outlet />
      </main>
    </div>
  )
}
