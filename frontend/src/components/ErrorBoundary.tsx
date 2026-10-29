import React from 'react'

/**
 * 路由级错误边界。
 *
 * 为什么必须有：React 18 里任何渲染期异常都会卸载整棵树（白屏），
 * 用户只看到空白页且没有任何线索。这里做到三件事：
 * 1. 接住异常，展示可操作的恢复入口（重试 / 回首页）；
 * 2. 把错误、组件栈、当前 URL 打到控制台，便于按 F12 反馈；
 * 3. 生成一个短错误码给用户，客服可以据此在日志里定位同一次故障。
 *
 * 注意：错误边界抓不到事件处理器/异步 Promise 里的异常，
 * 所以全局再挂 unhandledrejection 兜底（见 installGlobalErrorHandlers）。
 */
interface Props {
  children: React.ReactNode
}

interface State {
  error: Error | null
  info: string
  code: string
}

function shortCode(): string {
  return Math.random().toString(36).slice(2, 8).toUpperCase()
}

export default class ErrorBoundary extends React.Component<Props, State> {
  constructor(props: Props) {
    super(props)
    this.state = { error: null, info: '', code: '' }
  }

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error, code: shortCode() }
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error('[zhike] render error', this.state.code, error, info.componentStack)
    this.setState({ info: info.componentStack || '' })
  }

  private retry = () => {
    this.setState({ error: null, info: '', code: '' })
  }

  render() {
    const { error, code } = this.state
    if (!error) return this.props.children
    return (
      <div className="p-8" role="alert" aria-live="assertive">
        <h2 className="text-lg font-semibold">页面出错了</h2>
        <p className="text-sm text-muted mt-2">
          当前页面渲染失败，其它功能不受影响。可以先重试；持续失败请把错误码发给管理员。
        </p>
        <pre className="text-xs mt-3 p-3 rounded bg-[var(--panel-2)] overflow-auto max-h-40">
          {error.message}
          {'\n\n错误码：'}{code}
        </pre>
        <div className="flex gap-2 mt-4">
          <button className="btn" onClick={this.retry}>重试</button>
          <button className="btn secondary" onClick={() => { window.location.href = '/' }}>回到对话</button>
        </div>
      </div>
    )
  }
}

/** 全局兜底：未被 error boundary 捕获的异步异常（Promise rejection）。 */
export function installGlobalErrorHandlers(): void {
  if (typeof window === 'undefined') return
  window.addEventListener('unhandledrejection', (e) => {
    // 只记录，不弹窗骚扰用户：多数 rejection 是取消的请求
    console.error('[zhike] unhandled rejection', e.reason)
  })
}
