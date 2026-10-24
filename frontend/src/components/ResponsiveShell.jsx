import React, { createContext, useContext, useState } from 'react'
import {
  Link, useLocation, useNavigate,
} from 'react-router-dom'
import {
  MessageCircle, Headset, Settings, Menu, X,
  LayoutDashboard, MessagesSquare, ScrollText, ShieldAlert, ListChecks,
  Bot, BookOpen, Stamp, Moon, Sun, Search, Command,
} from 'lucide-react'
import { useMediaQuery } from '../hooks/useMediaQuery'
import Logo from './Logo'

/** 分组导航（Codex 式）：组名小字居上，条目紧凑排布。
 *  match 可选：自定义激活判定（默认 startsWith，end 表示全等）。 */
export const NAV_GROUPS = [
  {
    label: '工作台',
    items: [
      { to: '/dashboard', label: '运行总览', icon: <LayoutDashboard size={16} /> },
      { to: '/sessions', label: '会话中心', icon: <MessagesSquare size={16} /> },
      { to: '/operator', label: '坐席工作台', icon: <Headset size={16} /> },
      { to: '/', label: '访客对话', icon: <MessageCircle size={16} />, end: true },
    ],
  },
  {
    label: '智能体',
    items: [
      { to: '/persona', label: '机器人角色', icon: <Bot size={16} /> },
      { to: '/annotations', label: '标注回复', icon: <Stamp size={16} /> },
      { to: '/settings/kb', label: '知识库', icon: <BookOpen size={16} />, match: (p) => p.startsWith('/settings/kb') },
      { to: '/runs', label: '任务运行', icon: <ListChecks size={16} /> },
      { to: '/settings', label: '管理中心', icon: <Settings size={16} />, match: (p) => p.startsWith('/settings') && !p.startsWith('/settings/kb') },
    ],
  },
  {
    label: '审计',
    items: [
      { to: '/actions', label: '动作审计', icon: <ShieldAlert size={16} /> },
      { to: '/logs', label: '日志中心', icon: <ScrollText size={16} /> },
    ],
  },
]

export const NAV = NAV_GROUPS.flatMap((g) => g.items)

const Ctx = React.createContext({ drawerOpen: false, setDrawerOpen: () => {}, mobile: false })
export const useShell = () => useContext(Ctx)

function useTheme() {
  const [theme, setTheme] = useState(() => localStorage.getItem('zhike-theme') || 'light')
  React.useEffect(() => {
    document.documentElement.dataset.theme = theme
    localStorage.setItem('zhike-theme', theme)
  }, [theme])
  return [theme, () => setTheme((t) => (t === 'dark' ? 'light' : 'dark'))]
}

/** Ctrl/Cmd + K 命令面板（Codex 对标）：键盘直达任意页面 */
function CommandPalette({ close }) {
  const nav = useNavigate()
  const [kw, setKw] = useState('')
  const items = NAV_GROUPS.flatMap((g) => g.items.map((n) => ({ ...n, group: g.label })))
    .filter((n) => !kw || n.label.includes(kw) || n.group.includes(kw))
  React.useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') close() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [close])
  return (
    <>
      <div className="palette-mask" onClick={close} />
      <div className="palette">
        <div className="palette-input">
          <Search size={15} />
          <input autoFocus value={kw} onChange={(e) => setKw(e.target.value)} placeholder="跳转到…" />
          <kbd>Esc</kbd>
        </div>
        <div className="palette-list">
          {items.map((n) => (
            <button key={n.to + n.label} onClick={() => { nav(n.to); close() }}>
              {n.icon}
              <span>{n.label}</span>
              <em>{n.group}</em>
            </button>
          ))}
          {!items.length && <div className="palette-empty">没有匹配的页面</div>}
        </div>
      </div>
    </>
  )
}

function Brand({ onToggleTheme, theme }) {
  return (
    <div className="shell-brand">
      <Logo size={30} onDark withText subtitle="在线客服台" />
      <button className="theme-toggle" onClick={onToggleTheme} title={theme === 'dark' ? '切换亮色' : '切换暗色'}>
        {theme === 'dark' ? <Sun size={15} /> : <Moon size={15} />}
      </button>
    </div>
  )
}

function SideNav({ onNav }) {
  const loc = useLocation()
  return (
    <nav className="shell-nav">
      {NAV_GROUPS.map((g) => (
        <div className="nav-group" key={g.label}>
          <div className="nav-group-label">{g.label}</div>
          {g.items.map((n) => {
            const active = n.match ? n.match(loc.pathname) : n.end ? loc.pathname === n.to : loc.pathname.startsWith(n.to)
            return (
              <Link key={n.to + n.label} to={n.to} onClick={onNav} className={`shell-item ${active ? 'active' : ''}`}>
                {n.icon}
                <span>{n.label}</span>
              </Link>
            )
          })}
        </div>
      ))}
    </nav>
  )
}

export default function ResponsiveShell({ children }) {
  const mobile = useMediaQuery('(max-width: 960px)')
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [theme, toggleTheme] = useTheme()
  const [paletteOpen, setPaletteOpen] = useState(false)
  const loc = useLocation()
  const isApp = ['/', '/operator', '/persona', '/annotations', '/settings'].includes(loc.pathname) || loc.pathname.startsWith('/settings/')

  // Ctrl/Cmd + K 打开命令面板（Codex 对标）
  React.useEffect(() => {
    const onKey = (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        setPaletteOpen((o) => !o)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  React.useEffect(() => setDrawerOpen(false), [loc.pathname])

  const mainClass = `shell-main ${isApp ? 'is-app' : ''}`

  if (mobile) {
    return (
      <Ctx.Provider value={{ drawerOpen, setDrawerOpen, mobile: true }}>
        <div className={`app-shell ${isApp ? 'app-shell-fixed' : ''}`}>
          <header className="shell-top">
            <button className="shell-menu-btn" onClick={() => setDrawerOpen(true)} aria-label="打开菜单">
              <Menu size={19} />
            </button>
            <div className="shell-top-brand">
              <Logo size={24} withText />
            </div>
            <span style={{ width: 36 }} />
          </header>
          {drawerOpen && (
            <>
              <div className="shell-drawer-mask" onClick={() => setDrawerOpen(false)} />
              <div className="shell-drawer">
                <button className="shell-drawer-close" onClick={() => setDrawerOpen(false)} aria-label="关闭菜单">
                  <X size={16} />
                </button>
                <Brand onToggleTheme={toggleTheme} theme={theme} />
                <SideNav onNav={() => setDrawerOpen(false)} />
                <div className="shell-foot">知客 · MIT 开源</div>
              </div>
            </>
          )}
          <main className={mainClass}>{children}</main>
          {paletteOpen && <CommandPalette close={() => setPaletteOpen(false)} />}
        </div>
      </Ctx.Provider>
    )
  }

  return (
    <Ctx.Provider value={{ drawerOpen, setDrawerOpen, mobile: false }}>
      <div className={`app-shell ${isApp ? 'app-shell-fixed' : ''}`}>
        <aside className="shell-side">
          <Brand onToggleTheme={toggleTheme} theme={theme} />
          <SideNav />
          <button className="palette-hint" onClick={() => setPaletteOpen(true)} title="命令面板">
            <Command size={12} /> <span>K</span>
          </button>
          <div className="shell-foot">
            知客<br />在线客服台 · MIT
          </div>
        </aside>
        <main className={mainClass}>{children}</main>
        {paletteOpen && <CommandPalette close={() => setPaletteOpen(false)} />}
      </div>
    </Ctx.Provider>
  )
}
