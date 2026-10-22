import React, { createContext, useContext, useState } from 'react'
import { Link, useLocation } from 'react-router-dom'
import {
  MessageCircle, Headset, Settings, Menu, X,
  LayoutDashboard, MessagesSquare, ScrollText, ShieldAlert, ListChecks,
} from 'lucide-react'
import { useMediaQuery } from '../hooks/useMediaQuery'
import Logo from './Logo'

export const NAV = [
  { to: '/dashboard', label: '运行总览', icon: <LayoutDashboard size={17} /> },
  { to: '/sessions', label: '会话中心', icon: <MessagesSquare size={17} /> },
  { to: '/actions', label: '动作审计', icon: <ShieldAlert size={17} /> },
  { to: '/runs', label: '任务运行', icon: <ListChecks size={17} /> },
  { to: '/operator', label: '坐席工作台', icon: <Headset size={17} /> },
  { to: '/logs', label: '日志中心', icon: <ScrollText size={17} /> },
  { to: '/', label: '访客对话', icon: <MessageCircle size={17} />, end: true },
  { to: '/settings', label: '管理中心', icon: <Settings size={17} /> },
]

const Ctx = React.createContext({ drawerOpen: false, setDrawerOpen: () => {}, mobile: false })
export const useShell = () => useContext(Ctx)

function Brand() {
  return (
    <div className="shell-brand">
      <Logo size={30} onDark withText subtitle="在线客服台" />
    </div>
  )
}

function SideNav({ onNav }) {
  const loc = useLocation()
  return (
    <nav className="shell-nav">
      {NAV.map((n) => {
        const active = n.end ? loc.pathname === n.to : loc.pathname.startsWith(n.to)
        return (
          <Link key={n.to} to={n.to} onClick={onNav} className={`shell-item ${active ? 'active' : ''}`}>
            {n.icon}
            <span>{n.label}</span>
            <span className="si-dot" />
          </Link>
        )
      })}
    </nav>
  )
}

export default function ResponsiveShell({ children }) {
  const mobile = useMediaQuery('(max-width: 960px)')
  const [drawerOpen, setDrawerOpen] = useState(false)
  const loc = useLocation()
  const isApp = ['/', '/operator', '/settings'].includes(loc.pathname) || loc.pathname.startsWith('/settings/')

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
                <Brand />
                <SideNav onNav={() => setDrawerOpen(false)} />
                <div className="shell-foot">知客 · MIT 开源</div>
              </div>
            </>
          )}
          <main className={mainClass}>{children}</main>
        </div>
      </Ctx.Provider>
    )
  }

  return (
    <Ctx.Provider value={{ drawerOpen, setDrawerOpen, mobile: false }}>
      <div className={`app-shell ${isApp ? 'app-shell-fixed' : ''}`}>
        <aside className="shell-side">
          <Brand />
          <SideNav />
          <div className="shell-foot">
            知客<br />在线客服台 · MIT
          </div>
        </aside>
        <main className={mainClass}>{children}</main>
      </div>
    </Ctx.Provider>
  )
}
