import { useState } from 'react'
import { Lock, LogOut } from 'lucide-react'
import { useAppStore } from '../store/appStore'
import { isAuthError } from '../lib/queries'

/**
 * 管理令牌门禁：服务端设了 ADMIN_TOKEN 时，控制台必须登录才能看到数据。
 * 以前 http.get 不校验状态码，401 被当成正常响应，这里永远不会出现——现在是唯一入口。
 */
export function TokenGate({ error, onAuthed }: { error?: unknown; onAuthed?: () => void }) {
  const adminToken = useAppStore((s) => s.adminToken)
  const setAdminToken = useAppStore((s) => s.setAdminToken)
  const [input, setInput] = useState('')
  const [pending, setPending] = useState(false)

  const submit = () => {
    const t = input.trim()
    if (!t) return
    setPending(true)
    setAdminToken(t)
    // 令牌写入 store 后由调用方重新拉数据；这里不直接发请求，避免与各页面 queryKey 耦合
    setTimeout(() => {
      setPending(false)
      onAuthed?.()
    }, 30)
  }

  const hint = (() => {
    if (!error) return '该页面需要管理员令牌'
    if (isAuthError(error)) {
      const msg = (error as Error).message || ''
      if (msg.includes('未配置')) return '服务端未设置 ADMIN_TOKEN，管理接口已禁用。请在环境变量配置后重启。'
      return adminToken ? '令牌无效或已变更，请重新输入' : '该页面需要管理员令牌'
    }
    return (error as Error).message || '请求失败'
  })()

  return (
    <div className="max-w-[440px] mx-auto my-16 bg-panel border border-line-2 rounded-xl p-6 space-y-3">
      <div className="flex items-center gap-2">
        <Lock size={16} className="text-warn" />
        <span className="font-semibold text-sm">需要管理员令牌</span>
      </div>
      <p className="text-xs text-muted" style={{ lineHeight: 1.7 }}>{hint}</p>
      <div className="flex items-center gap-2">
        <input
          type="password"
          className="flex-1"
          placeholder="输入 ADMIN_TOKEN"
          value={input}
          autoFocus
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') submit() }}
        />
        <button className="btn sm" onClick={submit} disabled={!input.trim() || pending}>
          {pending ? '验证中…' : '登录'}
        </button>
      </div>
      {adminToken && (
        <button
          className="btn ghost sm"
          onClick={() => { setAdminToken(''); setInput(''); onAuthed?.() }}
        >
          <LogOut size={13} /> 清除已保存的令牌
        </button>
      )}
      <p className="text-[11px] text-muted">令牌只存在本机浏览器 localStorage，不会发送到除本服务以外的地址。</p>
    </div>
  )
}

export default TokenGate
