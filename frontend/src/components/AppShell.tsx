import { App as AntApp, Button, ConfigProvider, Divider, Tooltip, theme } from 'antd'
import { useEffect } from 'react'
import { useStoredBoolean } from '@/hooks/useStoredBoolean'
import zhCN from 'antd/locale/zh_CN'
import { Activity, Blocks, BookOpen, Bot, Cable, Moon, PanelLeftClose, Settings, Sun } from 'lucide-react'
import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { useTheme } from '@/hooks/useTheme'
import styles from './shell.module.css'

const navigation = [
  { to: '/chat', label: '对话', description: '与 Agent 一起完成任务', icon: Bot },
  { to: '/workflows', label: '工作流', description: '编排、运行与复用', icon: Cable },
  { to: '/skills', label: 'Skills', description: '管理指令与可复用能力', icon: BookOpen },
  { to: '/capabilities', label: 'SkillX', description: '工具与连接', icon: Blocks },
  { to: '/monitoring', label: '监控', description: '用量、质量与 Session 记录', icon: Activity },
]

export default function AppShell() {
  const { isDark, toggleTheme } = useTheme()
  const controls = isDark
    ? { hover: '#30343b', selected: '#343d49', selectedHover: '#414d5c', border: '#8b98a7', ring: 'rgba(139, 152, 167, .18)', text: '#ececec' }
    : { hover: '#f4f6f8', selected: '#edf1f5', selectedHover: '#e3eaf2', border: '#8695a7', ring: 'rgba(134, 149, 167, .16)', text: '#171717' }
  const { pathname } = useLocation()
  const navigationPath = pathname.startsWith('/components') ? '/workflows' : pathname
  const [collapsed, setCollapsed] = useStoredBoolean('skillx-navigation-collapsed')
  useEffect(() => {
    const page = navigationPath.startsWith('/settings') ? '设置' : navigation.find(item => navigationPath.startsWith(item.to))?.label
    document.title = page ? `${page} · SkillX 智能工作空间` : 'SkillX · 智能工作空间'
  }, [navigationPath])
  return <ConfigProvider locale={zhCN} button={{ autoInsertSpace: false }} theme={{
    algorithm: isDark ? theme.darkAlgorithm : theme.defaultAlgorithm,
    token: {
      colorPrimary: isDark ? '#ececec' : '#202020',
      colorTextLightSolid: isDark ? '#171717' : '#ffffff',
      colorBgBase: isDark ? '#212121' : '#ffffff',
      colorBgContainer: isDark ? '#262626' : '#ffffff',
      colorBorder: isDark ? '#424242' : '#e4e4e7',
      colorBorderSecondary: isDark ? '#343434' : '#e5e5e5',
      colorText: controls.text,
      colorTextSecondary: isDark ? '#b4b4b4' : '#666666',
      colorBgElevated: isDark ? '#292929' : '#ffffff',
      controlItemBgHover: controls.hover,
      controlItemBgActive: controls.selected,
      controlItemBgActiveHover: controls.selectedHover,
      controlOutline: controls.ring,
      controlOutlineWidth: 2,
      boxShadowSecondary: isDark ? '0 8px 28px rgba(0, 0, 0, .3)' : '0 8px 28px rgba(24, 39, 58, .1)',
      borderRadius: 10, controlHeight: 38, fontSize: 14,
      fontFamily: 'Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans SC", sans-serif',
    },
    components: {
      Button: { primaryShadow: 'none', defaultShadow: 'none' },
      Select: {
        optionSelectedBg: controls.selected, optionSelectedColor: controls.text,
        optionSelectedFontWeight: 500, optionActiveBg: controls.hover,
        optionPadding: '8px 12px', optionHeight: 36,
        activeBorderColor: controls.border, hoverBorderColor: controls.border,
        activeOutlineColor: controls.ring,
      },
      Input: { activeBorderColor: controls.border, hoverBorderColor: controls.border, activeShadow: `0 0 0 2px ${controls.ring}` },
      InputNumber: { activeBorderColor: controls.border, hoverBorderColor: controls.border, activeShadow: `0 0 0 2px ${controls.ring}` },
      Table: { headerBg: isDark ? '#2b2b2b' : '#f7f8fa', headerColor: isDark ? '#b4b4b4' : '#666666', rowHoverBg: controls.hover, borderColor: isDark ? '#343434' : '#e5e5e5' },
      Segmented: { trackBg: isDark ? '#171717' : '#f3f3f3' },
    },
  }}>
    <AntApp>
      <div className={`${styles.shell} ${collapsed ? styles.collapsed : ''}`}>
        <aside className={styles.sidebar} aria-label="工作空间导航">
          <div className={styles.sidebarHeader}>
            {collapsed ? (
              <Tooltip title="展开主导航">
                <button type="button" className={styles.expandBrand} aria-label="展开主导航"
                  aria-expanded={false} onClick={() => setCollapsed(false)}>
                  <img className={styles.logo} src="/favicon.svg" alt="SkillX" />
                </button>
              </Tooltip>
            ) : (
              <>
                <NavLink to="/chat" className={styles.brand}>
                  <img className={styles.logo} src="/favicon.svg" alt="SkillX" />
                  <strong>SkillX<span>智能工作空间</span></strong>
                </NavLink>
                <Tooltip title="折叠主导航">
                  <Button type="text" className={styles.collapseToggle} aria-label="折叠主导航"
                    aria-expanded={true} onClick={() => setCollapsed(true)}
                    icon={<PanelLeftClose size={18} strokeWidth={1.7} />} />
                </Tooltip>
              </>
            )}
          </div>
          <div className={styles.navCaption}>工作空间</div>
          <nav className={styles.navigation} aria-label="主导航">
            {navigation.map(({ to, label, description, icon: Icon }) => <NavLink key={to} to={to} aria-label={label} title={collapsed ? label : undefined}
              className={({ isActive }) => `${styles.navItem} ${isActive || (to === '/workflows' && navigationPath === '/workflows') ? styles.active : ''}`}>
              <Icon size={19} strokeWidth={1.7} /><span>{label}<small>{description}</small></span>
            </NavLink>)}
          </nav>
          <div className={styles.footer}>
            <Divider />
            <div className={styles.footerActions}>
            <Tooltip title="设置">
              <NavLink to="/settings" aria-label="设置" className={({isActive}) => `${styles.settingsIcon} ${isActive ? styles.active : ''}`}><Settings size={18} strokeWidth={1.7} /></NavLink>
            </Tooltip>
            <Tooltip title={isDark ? '切换到亮色主题' : '切换到暗色主题'}>
              <Button type="text" icon={isDark ? <Moon size={16} /> : <Sun size={16} />}
                aria-label={isDark ? '切换到亮色主题' : '切换到暗色主题'} aria-pressed={isDark} onClick={toggleTheme} />
            </Tooltip>
            </div>
          </div>
        </aside>
        <main className={styles.workspace}><Outlet /></main>
      </div>
    </AntApp>
  </ConfigProvider>
}
