import { useEffect, useState } from 'react'
import { Lock, LogOut, Copy, Check, RefreshCw, KeyRound } from 'lucide-react'
import { useAppStore } from '../store/appStore'
import { isAuthError } from '../lib/queries'
import { t } from '../lib/t'

/**
 * 管理门禁。
 *
 * 三种状态：
 * 1. 本机（回环地址）访问 → 自动从 /api/bootstrap/local-token 取令牌，用户什么都不用输；
 * 2. 远程访问 → 显示令牌输入框，并提示"令牌可在服务器本机控制台复制"；
 * 3. 服务端未配置 ADMIN_TOKEN → 说明如何开启。
 *
 * 自动获取失败绝不静默：能取到什么原因就显示什么，不能让用户对着空白页猜。
 */
export function TokenGate({ error, onAuthed }: { error?: unknown; onAuthed?: () => void }) {
  const adminToken = useAppStore((s) => s.adminToken)
  const setAdminToken = useAppStore((s) => s.setAdminToken)
  const [input, setInput] = useState('')
  const [pending, setPending] = useState(false)
  const [autoState, setAutoState] = useState<'idle' | 'trying' | 'failed'>('idle')
  const [autoReason, setAutoReason] = useState('')
  const [copied, setCopied] = useState(false)

  const autoLogin = async () => {
    setAutoState('trying')
    try {
      const r = await fetch('/api/bootstrap/local-token')
      const d = await r.json().catch(() => ({}))
      if (!r.ok) {
        setAutoState('failed')
        setAutoReason(d?.detail || `HTTP ${r.status}`)
        return
      }
      if (!d.available || !d.token) {
        setAutoState('failed')
        setAutoReason(d.reason || '服务端未提供令牌')
        return
      }
      if (d.token === adminToken) {
        // 令牌没变（只是上次查询失败）：不必重载，交给调用方重试
        setAutoState('idle')
        onAuthed?.()
        return
      }
      setAdminToken(d.token)
      setAutoState('idle')
      // 硬刷新：令牌写入 store 后，各页面已发出的 401 query 不会自动重跑，
      // 只调 onAuthed 会停在"正在校验"上（实测）。整页重载最稳且只发生一次。
      window.location.reload()
    } catch (e) {
      setAutoState('failed')
      setAutoReason((e as Error).message || '网络错误')
    }
  }

  // 挂载时总是先尝试本机引导，而不是「已有令牌就不试」。
  // 令牌轮换后 localStorage 里留着的是旧值，旧逻辑会直接显示输入框——
  // 用户看到的就是「每次进来都让我输令牌」（真实踩过）。
  useEffect(() => {
    autoLogin()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const submit = () => {
    const t = input.trim()
    if (!t) return
    setPending(true)
    setAdminToken(t)
    setTimeout(() => {
      setPending(false)
      onAuthed?.()
    }, 30)
  }

  const copySaved = async () => {
    try {
      await navigator.clipboard.writeText(adminToken)
      setCopied(true)
      setTimeout(() => setCopied(false), 1800)
    } catch {
      /* 剪贴板被拒时用户可手动选中 */
    }
  }

  const hint = (() => {
    if (!error) return '正在校验访问权限…'
    if (isAuthError(error)) {
      const msg = (error as Error).message || ''
      if (msg.includes('未配置')) return '服务端未设置 ADMIN_TOKEN，管理接口已禁用。'
      return adminToken ? '令牌无效或已变更。' : '需要访问令牌。'
    }
    return (error as Error).message || '请求失败'
  })()

  if (autoState === 'idle' || autoState === 'trying') {
    return (
      <div className="max-w-[440px] mx-auto my-16 bg-panel border border-line-2 rounded-xl p-6 flex items-center gap-2 text-sm">
        <RefreshCw size={15} className="animate-spin" /> 正在进入控制台…
      </div>
    )
  }

  // 不再提供手输令牌：本机走 /api/bootstrap/local-token 自动进入，
  // 远程访问由反代注入，输入框只会让用户每次进来都被拦一次。
  return (
    <div className="max-w-[440px] mx-auto my-16 bg-panel border border-line-2 rounded-xl p-6 space-y-3">
      <div className="flex items-center gap-2">
        <Lock size={16} className="text-warn" />
        <span className="font-semibold text-sm">{t('进不去控制台')}</span>
      </div>
      <p className="text-xs text-muted" style={{ lineHeight: 1.7 }}>{hint}</p>
      {autoState === 'failed' && autoReason && (
        <p className="text-[11px] text-muted">{autoReason}</p>
      )}
      <button className="btn sm" onClick={autoLogin}>
        <KeyRound size={13} /> {t('重新进入')}
      </button>
    </div>
  )
}

export default TokenGate
