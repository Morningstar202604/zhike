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

  // 首次挂载自动尝试：本机访问时用户应当完全无感
  useEffect(() => {
    if (!adminToken) autoLogin()
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

  if (autoState === 'trying') {
    return (
      <div className="max-w-[440px] mx-auto my-16 bg-panel border border-line-2 rounded-xl p-6 flex items-center gap-2 text-sm">
        <RefreshCw size={15} className="animate-spin" /> 正在进入控制台…
      </div>
    )
  }

  return (
    <div className="max-w-[440px] mx-auto my-16 bg-panel border border-line-2 rounded-xl p-6 space-y-3">
      <div className="flex items-center gap-2">
        <Lock size={16} className="text-warn" />
        <span className="font-semibold text-sm">{t('需要访问令牌')}</span>
      </div>
      <p className="text-xs text-muted" style={{ lineHeight: 1.7 }}>{hint}</p>

      {autoState === 'failed' && (
        <div className="text-[11px] text-muted border border-line-2 rounded-lg p-2" style={{ lineHeight: 1.6 }}>
          <div>未能自动登录：{autoReason}</div>
          <div style={{ marginTop: 4 }}>
            远程访问时，请在<b>{t('服务器本机')}</b>打开控制台，进入「管理中心 → 访问令牌」复制后粘贴到这里。
          </div>
        </div>
      )}

      <div className="flex items-center gap-2">
        <input
          type="text"
          className="flex-1"
          placeholder={t('粘贴访问令牌')}
          value={input}
          autoFocus
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') submit() }}
        />
        <button className="btn sm" onClick={submit} disabled={!input.trim() || pending}>
          {pending ? '验证中…' : '进入'}
        </button>
      </div>

      <button className="btn secondary sm" onClick={autoLogin}>
        <KeyRound size={13} /> 重新自动获取
      </button>

      {adminToken && (
        <div className="space-y-2">
          <div className="text-[11px] text-muted">{t('当前令牌（点击复制）')}</div>
          <button
            className="w-full text-left text-[11px] font-mono border border-line-2 rounded-lg p-2 break-all"
            onClick={copySaved}
            title={t('点击复制')}
          >
            {copied ? <span className="text-ok"><Check size={12} /> {t('已复制')}</span> : <><Copy size={12} /> {adminToken}</>}
          </button>
          <button
            className="btn ghost sm"
            onClick={() => { setAdminToken(''); setInput(''); onAuthed?.() }}
          >
            <LogOut size={13} /> 清除已保存的令牌
          </button>
        </div>
      )}

      <p className="text-[11px] text-muted">
        令牌存在本机浏览器 localStorage；本机访问时由服务端回环地址直接下发。
      </p>
    </div>
  )
}

export default TokenGate
