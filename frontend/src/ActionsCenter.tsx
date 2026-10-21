import { useState } from 'react'
import { ShieldAlert, CheckCircle2, XCircle, RefreshCw, Radio, Zap, GitBranch } from 'lucide-react'
import {
  useActionCatalog, useActionLogs, useApprovals, useApprovalMutations,
  useAuditVerify, useExecutorHealth, isAuthError,
} from './lib/queries'
import type { ActionLogEntry } from './lib/queries'
import { PageHead, EmptyState } from './ui'
import { Spinner, ErrorBanner } from './components/Spinner'
import TokenGate from './components/TokenGate'
import { useFeedback } from './components/Feedback'
import { ACTION_LABEL, ACTION_STATUS, RISK_LABEL, label } from './lib/labels'
import './pages2.css'

const STATUS_LABEL: Record<string, string> = {
  created: '已创建',
  pending_approval: '待审批',
  approved: '已批准',
  executed: '已执行',
  denied: '已拒绝',
  failed: '失败',
  expired: '已过期',
}

const STATUS_TONE: Record<string, string> = {
  executed: 'ok',
  approved: 'ok',
  pending_approval: 'warn',
  denied: 'danger',
  failed: 'danger',
  expired: 'muted',
  created: 'info',
}

const RISK_TONE: Record<number, string> = { 0: 'muted', 1: 'info', 2: 'danger' }

const FILTERS = [
  { key: '', label: '全部' },
  { key: 'executed', label: '已执行' },
  { key: 'pending_approval', label: '待审批' },
  { key: 'denied', label: '已拒绝' },
  { key: 'failed', label: '失败' },
]

function fmtTime(iso?: string | null): string {
  if (!iso) return '-'
  try {
    return new Date(iso).toLocaleString('zh-CN', {
      month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
    })
  } catch {
    return iso
  }
}

function paramsPreview(p: Record<string, unknown> | undefined): string {
  if (!p || Object.keys(p).length === 0) return '-'
  const parts = Object.entries(p).slice(0, 3).map(([k, v]) => `${k}=${String(v).slice(0, 24)}`)
  return parts.join(' ')
}

export default function ActionsCenter() {
  const [status, setStatus] = useState('')
  const [detail, setDetail] = useState<ActionLogEntry | null>(null)
  const catalog = useActionCatalog()
  const logs = useActionLogs({ status: status || undefined })
  const approvals = useApprovals('pending')
  const ap = useApprovalMutations()
  const audit = useAuditVerify()
  const execHealth = useExecutorHealth()
  const fb = useFeedback()

  const authErr = isAuthError(catalog.error) ? catalog.error : isAuthError(logs.error) ? logs.error : null
  if (authErr) {
    return <TokenGate error={authErr} onAuthed={() => { catalog.refetch(); logs.refetch(); approvals.refetch() }} />
  }

  const entries = logs.data?.entries ?? []
  const pending = approvals.data?.entries ?? []
  const cat = catalog.data

  const refreshAll = () => { logs.refetch(); approvals.refetch(); catalog.refetch() }

  return (
    <div className="p-4 md:p-6 max-w-[1400px] mx-auto w-full space-y-4 pb-24 md:pb-10">
      <PageHead
        title="动作审计"
        sub="Agent 与人工触发的每一次动作：谁、在什么权限下、做了什么、结果如何"
        right={
          <>
            {FILTERS.map((f) => (
              <button key={f.key} onClick={() => setStatus(f.key)}
                className={`mod-toggle ${status === f.key ? 'on' : ''}`}>{f.label}</button>
            ))}
            <button className="btn ghost sm" onClick={refreshAll} title="立即刷新"><RefreshCw size={13} /></button>
          </>
        }
      />

      {cat && (
        <div className="bg-panel border border-line-2 rounded-xl p-4 flex flex-wrap items-center gap-x-6 gap-y-2 text-sm">
          <div className="flex items-center gap-2">
            <ShieldAlert size={15} className="text-brand" />
            <span className="font-semibold">权限档位</span>
            <span className={`badge ${cat.agent_tier >= 2 ? 'warn' : 'info'}`}>{cat.tier_label}</span>
          </div>
          <span className="badge muted">只读 {cat.risks.L0}</span>
          <span className="badge info">可逆写 {cat.risks.L1}</span>
          <span className={`badge ${cat.l2_enabled ? 'danger' : 'muted'}`}>
            破坏性 {cat.risks.L2} · {cat.l2_enabled ? '已启用' : '未启用'}
          </span>
          <span className={`badge shrink-0 ${execHealth.data?.online ? 'ok' : 'muted'}`}>
            <Radio size={11} /> 执行器{execHealth.data?.online ? '在线' : execHealth.data?.error ? '离线' : '…'}
          </span>
          <button className="btn ghost sm ml-auto" onClick={() => audit.mutate()} disabled={audit.isPending}>
            <GitBranch size={13} /> {audit.isPending ? '校验中…' : '校验审计链'}
          </button>
          {audit.data && (
            <span className={`badge ${audit.data.ok ? 'ok' : 'danger'}`}>
              {audit.data.ok ? `完整 · ${audit.data.checked} 条` : `断链 @ #${audit.data.broken_at}`}
            </span>
          )}
        </div>
      )}

      {catalog.isLoading && <Spinner full label="加载动作目录…" />}
      {catalog.error && !isAuthError(catalog.error) && (
        <ErrorBanner message={(catalog.error as Error).message} onRetry={() => catalog.refetch()} />
      )}

      {pending.length > 0 && (
        <div className="bg-warn-soft border border-warn rounded-xl p-4 space-y-2">
          <div className="flex items-center gap-2 text-sm font-semibold">
            <ShieldAlert size={15} className="text-warn" />
            待审批的破坏性动作（{pending.length}）
          </div>
          {pending.map((a) => (
            <div key={a.id} className="bg-panel border border-line-2 rounded-lg p-3 flex flex-wrap items-center gap-3">
              <span className="badge danger">{a.action}</span>
              <span className="text-xs flex-1 min-w-[200px]">{a.summary}</span>
              <span className="text-[11px] text-muted">过期 {fmtTime(a.expires_at)}</span>
              <button className="btn sm" onClick={() => ap.approve.mutate(a.id)} disabled={ap.approve.isPending}>
                <CheckCircle2 size={13} /> 批准
              </button>
              <button className="btn secondary sm"
                onClick={async () => {
                  const reason = await fb.prompt('拒绝原因（可留空）', '', {
                    title: '拒绝动作',
                    confirmText: '拒绝',
                  })
                  if (reason === null) return
                  ap.deny.mutate({ id: a.id, reason })
                }}
                disabled={ap.deny.isPending}>
                <XCircle size={13} /> 拒绝
              </button>
            </div>
          ))}
          {ap.approve.error && (
            <ErrorBanner message={(ap.approve.error as Error).message} onRetry={() => approvals.refetch()} />
          )}
          {ap.deny.error && (
            <ErrorBanner message={(ap.deny.error as Error).message} onRetry={() => approvals.refetch()} />
          )}
        </div>
      )}

      {logs.isLoading && <Spinner full label="加载动作日志…" />}
      {logs.error && !isAuthError(logs.error) && (
        <ErrorBanner message={(logs.error as Error).message} onRetry={() => logs.refetch()} />
      )}

      {!logs.isLoading && entries.length === 0 && (
        <EmptyState icon={<Zap size={22} />} title="暂无动作记录"
          desc="Agent 调用工具、管理员执行动作都会实时出现在这里。" />
      )}

      {entries.length > 0 && (
        <div className="tbl-wrap">
          <table className="dt">
            <thead>
              <tr>
                <th>时间</th><th>动作</th><th>风险</th><th>触发者</th><th>档位</th>
                <th>参数</th><th>状态</th><th>耗时</th><th></th>
              </tr>
            </thead>
            <tbody>
              {entries.map((e) => (
                <tr key={e.id} className="cursor-pointer" onClick={() => setDetail(e)}>
                  <td className="muted">{fmtTime(e.created_at)}</td>
                  <td>
                    <span className="font-medium">{label(ACTION_LABEL, e.action)}</span>
                    <code className="muted" style={{ fontSize: 10.5, marginLeft: 6 }}>{e.action}</code>
                  </td>
                  <td><span className={`badge ${RISK_TONE[e.risk] || 'muted'}`}>{RISK_LABEL[e.risk] || e.risk}</span></td>
                  <td className="raw" title={e.principal}>{e.principal}</td>
                  <td className="muted">{e.effective_tier ?? '-'}</td>
                  <td className="raw" title={paramsPreview(e.params)}>{paramsPreview(e.params)}</td>
                  <td><span className={`badge ${STATUS_TONE[e.status] || 'muted'}`}>{STATUS_LABEL[e.status] || e.status}</span></td>
                  <td className="muted">{e.duration_ms != null ? `${e.duration_ms}ms` : '-'}</td>
                  <td><button className="btn ghost sm" onClick={(ev) => { ev.stopPropagation(); setDetail(e) }}>详情</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {detail && (
        <div className="drawer-mask" onClick={() => setDetail(null)}>
          <div className="drawer" onClick={(e) => e.stopPropagation()}>
            <div className="drawer-head">
              <h3>动作详情</h3>
              <button className="btn ghost sm" onClick={() => setDetail(null)}><XCircle size={14} /></button>
            </div>
            <div className="drawer-body space-y-3 text-xs">
              <div>
                <span className="text-muted">动作：</span>{label(ACTION_LABEL, detail.action)}
                <code className="muted" style={{ marginLeft: 6 }}>{detail.action}</code>
              </div>
              <div><span className="text-muted">状态：</span>
                <span className={`badge ${STATUS_TONE[detail.status] || 'muted'}`}>{STATUS_LABEL[detail.status] || detail.status}</span>
              </div>
              <div><span className="text-muted">触发者：</span>{detail.principal}（有效档位 {detail.effective_tier ?? '-'}）</div>
              <div><span className="text-muted">时间：</span>{fmtTime(detail.created_at)}
                {detail.executed_at && ` → ${fmtTime(detail.executed_at)}`}</div>
              <div><span className="text-muted">参数（已打码）：</span>
                <pre style={{ whiteSpace: 'pre-wrap', margin: '4px 0', fontSize: 11 }}>
                  {JSON.stringify(detail.params, null, 2)}
                </pre>
              </div>
              {detail.result != null && (
                <div><span className="text-muted">结果：</span>
                  <pre style={{ whiteSpace: 'pre-wrap', margin: '4px 0', fontSize: 11 }}>
                    {JSON.stringify(detail.result, null, 2)}
                  </pre>
                </div>
              )}
              {detail.error && (
                <div className="hint err">{detail.error}</div>
              )}
              {detail.approval_id && (
                <div><span className="text-muted">关联审批：</span><code>{detail.approval_id}</code></div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
