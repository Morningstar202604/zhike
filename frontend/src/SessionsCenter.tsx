import { useState } from 'react'
import { Link } from 'react-router-dom'
import { MessagesSquare, X, RefreshCw, Headset, Inbox } from 'lucide-react'
import { http, useAdminSessions, useCloseSession } from './lib/queries'
import type { AgentMsg, SessionRow } from './types/api'
import { Spinner, ErrorBanner } from './components/Spinner'
import { EmptyState, PageHead } from './ui'
import './pages2.css'

const FILTERS = [
  { key: '', label: '全部' },
  { key: 'ongoing', label: '进行中' },
  { key: 'pending_agent', label: '待接管' },
  { key: 'agent_handled', label: '已接管' },
  { key: 'closed', label: '已关闭' },
]

const STATUS_LABELS: Record<string, string> = {
  ongoing: '进行中',
  pending_agent: '待接管',
  agent_handled: '坐席处理中',
  closed: '已关闭',
}

const STATUS_TONE: Record<string, string> = {
  ongoing: 'info',
  pending_agent: 'warn',
  agent_handled: 'ok',
  closed: 'muted',
}

function fmtTime(iso?: string | null): string {
  if (!iso) return '-'
  try {
    return new Date(iso).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false })
  } catch {
    return iso
  }
}

export default function SessionsCenter() {
  const [status, setStatus] = useState('')
  const [active, setActive] = useState<SessionRow | null>(null)
  const [dialog, setDialog] = useState<AgentMsg[]>([])
  const [loadingDialog, setLoadingDialog] = useState(false)
  const sessions = useAdminSessions(status || undefined)
  const close = useCloseSession()

  const open = async (s: SessionRow) => {
    setActive(s)
    setLoadingDialog(true)
    try {
      const d = await http.get<{ messages: AgentMsg[] }>(`/api/sessions/${s.id}/messages`)
      setDialog(d.messages || [])
    } catch {
      setDialog([])
    } finally {
      setLoadingDialog(false)
    }
  }

  const doClose = async () => {
    if (!active) return
    try {
      await close.mutateAsync(active.id)
      setActive({ ...active, status: 'closed' })
      await sessions.refetch()
    } catch {
      /* 错误经 mutation 暴露 */
    }
  }

  const items = sessions.data?.items ?? []

  return (
    <div className="p-4 md:p-6 max-w-[1400px] mx-auto w-full space-y-4 pb-24 md:pb-10">
      <PageHead
        title="会话中心"
        sub={`全部会话（含渠道来源）· 10 秒自动刷新 · 共 ${sessions.data?.total ?? '…'} 条`}
        right={
          <>
            {FILTERS.map((f) => (
              <button
                key={f.key}
                onClick={() => setStatus(f.key)}
                className={`mod-toggle ${status === f.key ? 'on' : ''}`}
              >
                {f.label}
              </button>
            ))}
            <button className="btn ghost sm" onClick={() => sessions.refetch()} title="立即刷新">
              <RefreshCw size={13} />
            </button>
          </>
        }
      />

      {sessions.isLoading && <Spinner full label="加载会话…" />}
      {sessions.error && <ErrorBanner message={(sessions.error as Error).message} onRetry={() => sessions.refetch()} />}

      {!sessions.isLoading && items.length === 0 && (
        <EmptyState icon={<Inbox size={22} />} title="没有匹配的会话" desc="换一个筛选条件，或去对话页/群里产生一条新会话。" />
      )}

      {items.length > 0 && (
        <div className="tbl-wrap">
          <table className="dt">
            <thead>
              <tr>
                <th>开始时间</th>
                <th>渠道</th>
                <th>访客</th>
                <th>状态</th>
                <th>消息</th>
                <th>升级原因</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {items.map((s) => (
                <tr key={s.id} className="cursor-pointer" onClick={() => open(s)}>
                  <td className="muted">{fmtTime(s.started_at)}</td>
                  <td><span className="badge info">{s.channel}</span></td>
                  <td className="raw" title={s.visitor_id}>{(s.visitor_id || '-').slice(0, 24)}</td>
                  <td>
                    <span className={`badge ${STATUS_TONE[s.status] || 'muted'}`}>
                      {STATUS_LABELS[s.status] || s.status}
                    </span>
                  </td>
                  <td>{s.message_count}</td>
                  <td className="muted">{s.escalate_reason || '-'}</td>
                  <td>
                    <button
                      className="btn ghost sm"
                      onClick={(e) => { e.stopPropagation(); open(s) }}
                    >
                      查看
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {active && (
        <div className="drawer-mask" onClick={() => setActive(null)}>
          <div className="drawer" onClick={(e) => e.stopPropagation()}>
            <div className="drawer-head">
              <h3>会话 #{active.id.slice(0, 8)} · {active.channel}</h3>
              <div className="flex gap-2">
                <Link to="/operator" className="btn ghost sm"><Headset size={13} /> 坐席台</Link>
                {active.status !== 'closed' && (
                  <button className="btn secondary sm" onClick={doClose} disabled={close.isPending}>
                    <X size={13} /> {close.isPending ? '关闭中…' : '关闭会话'}
                  </button>
                )}
                <button className="btn ghost sm" aria-label="关闭" title="关闭" onClick={() => setActive(null)}><X size={14} /></button>
              </div>
            </div>
            <div className="drawer-body space-y-3 max-h-[60vh] overflow-auto">
              {close.error && <ErrorBanner message={(close.error as Error).message} onRetry={() => sessions.refetch()} />}
              {loadingDialog && <Spinner label="加载对话…" />}
              {!loadingDialog && dialog.length === 0 && <div className="empty">该会话暂无消息</div>}
              {dialog.map((m, i) => (
                <div key={m.id ?? m.ts ?? i} className={`msg-row ${m.role === 'visitor' ? 'visitor' : m.role === 'operator' ? 'operator' : 'agent'}`}>
                  <div className="msg-body">
                    <div className="bubble">
                      <span className="text-[10px] text-muted mr-2">
                        {m.role === 'visitor' ? '访客' : m.role === 'operator' ? '坐席' : 'Agent'}
                      </span>
                      {m.content}
                    </div>
                  </div>
                </div>
              ))}
              {active.escalate_reason && (
                <div className="text-xs text-muted">升级原因：{active.escalate_reason} · 开始 {fmtTime(active.started_at)}</div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
