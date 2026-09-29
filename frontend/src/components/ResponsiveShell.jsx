import React, { createContext, useContext, useState } from 'react'
import { Link, useLocation } from 'react-router-dom'
import {
  MessageCircle, Headset, Settings, Menu, X,
  LayoutDashboard, MessagesSquare, ScrollText,
} from 'lucide-react'
import { useMediaQuery } from '../hooks/useMediaQuery'

export const NAV = [
  { to: '/dashboard', label: '运行总览', icon: <LayoutDashboard size={17} /> },
  { to: '/sessions', label: '会话中心', icon: <MessagesSquare size={17} /> },
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
      <span className="shell-brand-logo">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
          <path d="M12 2l2.1 6.2L20 10l-5.9 1.8L12 18l-2.1-6.2L4 10l5.9-1.8L12 2z" />
        </svg>
      </span>
      <span>
        <span className="shell-brand-name">知客 Zhike</span>
        <span className="shell-brand-sub">在线客服 Agent 控制台</span>
      </span>
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
              <span className="shell-top-logo">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
                  <path d="M12 2l2.1 6.2L20 10l-5.9 1.8L12 18l-2.1-6.2L4 10l5.9-1.8L12 2z" />
                </svg>
              </span>
              知客 Zhike
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
                <div className="shell-foot">知客 Zhike · MIT</div>
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
            知客 Zhike<br />在线客服 Agent · MIT
          </div>
        </aside>
        <main className={mainClass}>{children}</main>
      </div>
    </Ctx.Provider>
  )
}
