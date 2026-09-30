import { App as AntApp, Button, ConfigProvider, Divider, Tooltip, theme } from 'antd'
import { useEffect } from 'react'
import { useStoredBoolean } from '@/hooks/useStoredBoolean'
import zhCN from 'antd/locale/zh_CN'
import { Blocks, Bot, Boxes, Cable, Moon, PanelLeftClose, PanelLeftOpen, Plus, Settings, Sun } from 'lucide-react'
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { useTheme } from '@/hooks/useTheme'
import styles from './shell.module.css'

const navigation = [
  { to: '/chat', label: '对话', description: '与 Agent 一起完成任务', icon: Bot },
  { to: '/components', label: '组件', description: '可复用的任务能力', icon: Boxes },
  { to: '/workflows', label: '工作流', description: '编排与发布', icon: Cable },
  { to: '/capabilities', label: '能力', description: '工具与连接', icon: Blocks },
  { to: '/settings', label: '系统', description: '模型与运行时', icon: Settings },
]

export default function AppShell() {
  const { isDark, toggleTheme } = useTheme()
  const navigate = useNavigate()
  const { pathname } = useLocation()
  const [collapsed, setCollapsed] = useStoredBoolean('skillx-navigation-collapsed')
  useEffect(() => {
    const page = navigation.find(item => pathname.startsWith(item.to))?.label
    document.title = page ? `${page} · SkillX 智能工作空间` : 'SkillX · 智能工作空间'
  }, [pathname])
  return <ConfigProvider locale={zhCN} button={{ autoInsertSpace: false }} theme={{
    algorithm: isDark ? theme.darkAlgorithm : theme.defaultAlgorithm,
    token: {
      colorPrimary: isDark ? '#ececec' : '#202020',
      colorTextLightSolid: isDark ? '#171717' : '#ffffff',
      colorBgBase: isDark ? '#212121' : '#ffffff',
      colorBgContainer: isDark ? '#262626' : '#ffffff',
      colorBorder: isDark ? '#424242' : '#e4e4e7',
      borderRadius: 10, controlHeight: 38, fontSize: 14,
      fontFamily: 'Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans SC", sans-serif',
    },
    components: {
      Button: { primaryShadow: 'none', defaultShadow: 'none' },
      Select: { optionSelectedBg: isDark ? '#424242' : '#f0f0f0' },
      Segmented: { trackBg: isDark ? '#171717' : '#f3f3f3' },
    },
  }}>
    <AntApp>
      <div className={`${styles.shell} ${collapsed ? styles.collapsed : ''}`}>
        <aside className={styles.sidebar} aria-label="工作空间导航">
          <Tooltip title={collapsed ? '展开主导航' : '折叠主导航'}>
            <Button type="text" className={styles.collapseToggle} aria-label={collapsed ? '展开主导航' : '折叠主导航'}
              aria-expanded={!collapsed} onClick={() => setCollapsed(!collapsed)}
              icon={collapsed ? <PanelLeftOpen size={17} /> : <PanelLeftClose size={17} />} />
          </Tooltip>
          <NavLink to="/chat" className={styles.brand}>
            <img className={styles.logo} src="/favicon.svg" alt="SkillX" />
            <strong>SkillX<span>智能工作空间</span></strong>
          </NavLink>
          <Button className={styles.newChat} icon={<Plus size={16} />} onClick={() => navigate('/chat')} aria-label="新对话">{collapsed ? null : '新对话'}</Button>
          <div className={styles.navCaption}>工作空间</div>
          <nav className={styles.navigation} aria-label="主导航">
            {navigation.map(({ to, label, description, icon: Icon }) => <NavLink key={to} to={to} aria-label={label} title={collapsed ? label : undefined}
              className={({ isActive }) => `${styles.navItem} ${isActive ? styles.active : ''}`}>
              <Icon size={19} strokeWidth={1.7} /><span>{label}<small>{description}</small></span>
            </NavLink>)}
            <Button className={styles.mobileTheme} type="text" icon={isDark ? <Moon size={18} /> : <Sun size={18} />}
              aria-label={isDark ? '切换到亮色主题' : '切换到暗色主题'} onClick={toggleTheme} />
          </nav>
          <div className={styles.footer}>
            <Divider />
            <Tooltip title={isDark ? '切换到亮色主题' : '切换到暗色主题'}>
              <Button block type="text" icon={isDark ? <Moon size={16} /> : <Sun size={16} />}
                aria-label={isDark ? '切换到亮色主题' : '切换到暗色主题'} aria-pressed={isDark} onClick={toggleTheme} />
            </Tooltip>
          </div>
        </aside>
        <main className={styles.workspace}><Outlet /></main>
      </div>
    </AntApp>
  </ConfigProvider>
}
