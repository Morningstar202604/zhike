import { useState } from 'react'
import { Play, RefreshCw, ListChecks, CheckCircle2, XCircle, Clock } from 'lucide-react'
import { useAgentRuns, useAgentRunDetail, useStartAgentRun, isAuthError } from './lib/queries'
import type { AgentRun } from './lib/queries'
import { PageHead, EmptyState } from './ui'
import { Spinner, ErrorBanner } from './components/Spinner'
import TokenGate from './components/TokenGate'
import './pages2.css'

const STATUS_LABEL: Record<string, string> = {
  running: '运行中',
  done: '已完成',
  max_steps: '步数用尽',
  budget_exceeded: '超预算',
  cancelled: '已取消',
  error: '出错',
}

const STATUS_TONE: Record<string, string> = {
  running: 'info',
  done: 'ok',
  max_steps: 'warn',
  budget_exceeded: 'warn',
  cancelled: 'muted',
  error: 'danger',
}

const STEP_TONE: Record<string, string> = {
  done: 'ok',
  denied: 'danger',
  pending_approval: 'warn',
  failed: 'danger',
  blocked: 'muted',
}

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

export default function AgentRuns() {
  const [goal, setGoal] = useState('')
  const [selected, setSelected] = useState<string | null>(null)
  const runs = useAgentRuns()
  const detail = useAgentRunDetail(selected)
  const start = useStartAgentRun()

  const authErr = isAuthError(runs.error) ? runs.error : null
  if (authErr) {
    return <TokenGate error={authErr} onAuthed={() => runs.refetch()} />
  }

  const entries = runs.data?.entries ?? []

  return (
    <div className="p-4 md:p-6 max-w-[1400px] mx-auto w-full space-y-4 pb-24 md:pb-10">
      <PageHead
        title="任务运行"
        sub="让 Agent 用多步工具调用完成一个目标 · 每步的思考与观察都可回放"
        right={<button className="btn ghost sm" onClick={() => runs.refetch()} title="立即刷新"><RefreshCw size={13} /></button>}
      />

      <div className="bg-panel border border-line-2 rounded-xl p-4 space-y-2">
        <div className="flex items-center gap-2 text-sm font-semibold">
          <Play size={15} className="text-brand" /> 发起一个任务
        </div>
        <div className="flex flex-col md:flex-row gap-2">
          <input
            className="flex-1"
            placeholder="例如：看一下 C:/work 目录里有什么，把结构整理成一段说明"
            value={goal}
            onChange={(e) => setGoal(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') start.mutate({ goal, principal: 'admin' }) }}
          />
          <button
            className="btn"
            disabled={!goal.trim() || start.isPending}
            onClick={() => start.mutate({ goal, principal: 'admin' })}
          >
            {start.isPending ? '执行中…' : '开始'}
          </button>
        </div>
        <p className="text-[11px] text-muted">
          任务受步数/工具调用/时长预算限制；破坏性动作仍会停下来等人工确认。
        </p>
        {start.error && <ErrorBanner message={(start.error as Error).message} onRetry={() => start.reset()} />}
        {start.data && (
          <div className="text-xs">
            <span className={`badge ${STATUS_TONE[start.data.status] || 'muted'}`}>
              {STATUS_LABEL[start.data.status] || start.data.status}
            </span>
            <span className="ml-2">{start.data.answer}</span>
          </div>
        )}
      </div>

      {runs.isLoading && <Spinner full label="加载任务…" />}
      {runs.error && !isAuthError(runs.error) && (
        <ErrorBanner message={(runs.error as Error).message} onRetry={() => runs.refetch()} />
      )}

      {!runs.isLoading && entries.length === 0 && (
        <EmptyState icon={<ListChecks size={22} />} title="还没有任务记录"
          desc="在上面输入一个目标，Agent 会自己规划步骤并调用工具。" />
      )}

      {entries.length > 0 && (
        <div className="tbl-wrap">
          <table className="dt">
            <thead>
              <tr><th>开始</th><th>目标</th><th>触发者</th><th>状态</th><th>档位</th><th></th></tr>
            </thead>
            <tbody>
              {entries.map((r: AgentRun) => (
                <tr key={r.id} className="cursor-pointer" onClick={() => setSelected(r.id)}>
                  <td className="muted">{fmtTime(r.started_at)}</td>
                  <td className="raw" title={r.goal}>{r.goal}</td>
                  <td className="raw">{r.principal}</td>
                  <td><span className={`badge ${STATUS_TONE[r.status] || 'muted'}`}>{STATUS_LABEL[r.status] || r.status}</span></td>
                  <td className="muted">{r.tier ?? '-'}</td>
                  <td><button className="btn ghost sm" onClick={(e) => { e.stopPropagation(); setSelected(r.id) }}>步骤</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {selected && (
        <div className="drawer-mask" onClick={() => setSelected(null)}>
          <div className="drawer" onClick={(e) => e.stopPropagation()}>
            <div className="drawer-head">
              <h3>任务步骤</h3>
              <button className="btn ghost sm" onClick={() => setSelected(null)}><XCircle size={14} /></button>
            </div>
            <div className="drawer-body space-y-3">
              {detail.isLoading && <Spinner label="加载步骤…" />}
              {detail.error && <ErrorBanner message={(detail.error as Error).message} onRetry={() => detail.refetch()} />}
              {(detail.data?.steps ?? []).map((s) => (
                <div key={s.id} className="bg-panel border border-line-2 rounded-lg p-3 text-xs space-y-1">
                  <div className="flex items-center gap-2">
                    <span className="badge muted">第 {s.step_no} 步</span>
                    {s.action && <code style={{ fontSize: 11 }}>{s.action}</code>}
                    <span className={`badge ${STEP_TONE[s.status] || 'muted'}`}>{s.status}</span>
                    <span className="text-muted ml-auto"><Clock size={11} /> {s.duration_ms}ms</span>
                  </div>
                  {s.thought && <div className="text-muted">思考：{s.thought}</div>}
                  {s.observation && (
                    <pre style={{ whiteSpace: 'pre-wrap', margin: 0, fontSize: 11, maxHeight: 160, overflow: 'auto' }}>
                      {s.observation}
                    </pre>
                  )}
                </div>
              ))}
              {detail.data && detail.data.steps.length === 0 && (
                <div className="empty">该任务没有记录步骤</div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
