import React, { lazy, Suspense } from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter, Routes, Route } from 'react-router-dom'
import { QueryClientProvider } from '@tanstack/react-query'
import { queryClient } from './lib/queryClient'
import ResponsiveShell from './components/ResponsiveShell'
import { FeedbackProvider } from './components/Feedback'
import { Spinner } from './components/Spinner'
import './i18n'
import './index.css'
import './styles.css'

// 路由级代码分割（React.lazy + Suspense）
const VisitorChat = lazy(() => import('./VisitorChat'))
const OperatorDesk = lazy(() => import('./OperatorDesk'))
const Settings = lazy(() => import('./Settings'))
const Dashboard = lazy(() => import('./Dashboard'))
const LogsCenter = lazy(() => import('./LogsCenter'))
const SessionsCenter = lazy(() => import('./SessionsCenter'))
const ActionsCenter = lazy(() => import('./ActionsCenter'))
const AgentRuns = lazy(() => import('./AgentRuns'))
const BotPersona = lazy(() => import('./BotPersona'))
const AnnotationCenter = lazy(() => import('./AnnotationCenter'))
const Reminders = lazy(() => import('./Reminders'))

class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props)
    this.state = { error: null }
  }

  static getDerivedStateFromError(error) {
    return { error }
  }

  componentDidCatch(error, info) {
    // 保留控制台可见的堆栈，避免"白屏且无任何线索"
    console.error('render error', error, info)
  }

  render() {
    if (this.state.error) {
      return (
        <div className="p-8">
          <h2 className="text-lg font-semibold">页面出错了</h2>
          <p className="text-sm text-muted mt-2">{(this.state.error && this.state.error.message) || '未知错误'}</p>
          <button className="btn mt-4" onClick={() => window.location.reload()}>重新加载</button>
        </div>
      )
    }
    return this.props.children
  }
}

function NotFound() {
  return (
    <div className="p-8">
      <h2 className="text-lg font-semibold">页面不存在</h2>
      <p className="text-sm text-muted mt-2">地址可能写错了，或者功能已经改名。</p>
      <Link className="btn mt-4" to="/">回到对话</Link>
    </div>
  )
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <FeedbackProvider>
          <ResponsiveShell>
            <Suspense fallback={<Spinner full />}>
              <ErrorBoundary>
              <Routes>
                <Route path="/" element={<VisitorChat />} />
                <Route path="/dashboard" element={<Dashboard />} />
                <Route path="/reminders" element={<Reminders />} />
                <Route path="/persona" element={<BotPersona />} />
                <Route path="/annotations" element={<AnnotationCenter />} />
                <Route path="/sessions" element={<SessionsCenter />} />
                <Route path="/actions" element={<ActionsCenter />} />
                <Route path="/runs" element={<AgentRuns />} />
                <Route path="/logs" element={<LogsCenter />} />
                <Route path="/operator" element={<OperatorDesk />} />
                <Route path="/settings" element={<Settings />} />
                <Route path="/settings/:tab" element={<Settings />} />
                <Route path="*" element={<NotFound />} />
              </Routes>
              </ErrorBoundary>
            </Suspense>
          </ResponsiveShell>
        </FeedbackProvider>
      </BrowserRouter>
    </QueryClientProvider>
  )
}

ReactDOM.createRoot(document.getElementById('root')).render(<App />)
