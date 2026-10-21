import { useState } from 'react'
import {
  Play, RefreshCw, ListChecks, Clock, XCircle, Square, Loader2, ChevronRight,
} from 'lucide-react'
import {
  useAgentRuns, useAgentRunDetail, useStartAgentRun, useCancelAgentRun, isAuthError,
} from './lib/queries'
import type { AgentRun } from './lib/queries'
import { PageHead, EmptyState } from './ui'
import { Spinner, ErrorBanner } from './components/Spinner'
import TokenGate from './components/TokenGate'
import { ACTION_LABEL, label } from './lib/labels'
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

const STEP_LABEL: Record<string, string> = {
  done: '完成',
  denied: '被拒绝',
  pending_approval: '待审批',
  failed: '失败',
  blocked: '已拦截',
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

function fmtDuration(start?: string | null, end?: string | null): string {
  if (!start) return '-'
  const from = new Date(start).getTime()
  const to = end ? new Date(end).getTime() : Date.now()
  if (Number.isNaN(from)) return '-'
  const s = Math.max(0, (to - from) / 1000)
  return s < 60 ? `${s.toFixed(1)}s` : `${Math.floor(s / 60)}m${Math.round(s % 60)}s`
}

type Usage = { steps?: number; tool_calls?: number; tokens?: number; cost?: number }

function StepCard({ step }: { step: { id: number; step_no: number; thought: string; action: string | null; observation: string; status: string; duration_ms: number } }) {
  // 观察内容默认折叠：一屏能看完整条任务轨迹，需要时再展开细节
  const [open, setOpen] = useState(false)
  return (
    <div className="run-step">
      <div className="run-step-head">
        <span className="run-step-no">{step.step_no}</span>
        <span className={`badge ${STEP_TONE[step.status] || 'muted'}`}>{STEP_LABEL[step.status] || step.status}</span>
        {step.action
          ? <span className="run-step-action">{label(ACTION_LABEL, step.action)} <code>{step.action}</code></span>
          : <span className="run-step-action muted">直接回答</span>}
        <span className="run-step-ms"><Clock size={11} /> {step.duration_ms}ms</span>
      </div>
      {step.thought && <div className="run-step-thought">{step.thought}</div>}
      {step.observation && (
        <>
          <button
            type="button"
            className={`run-step-toggle ${open ? 'open' : ''}`}
            onClick={() => setOpen((v) => !v)}
            aria-expanded={open}
          >
            <ChevronRight size={11} />
            {open ? '收起观察结果' : '查看观察结果'}
          </button>
          {open && <pre className="run-step-obs">{step.observation}</pre>}
        </>
      )}
    </div>
  )
}

export default function AgentRuns() {
  const [goal, setGoal] = useState('')
  const [selected, setSelected] = useState<string | null>(null)
  const runs = useAgentRuns()
  const detail = useAgentRunDetail(selected)
  const start = useStartAgentRun()
  const cancel = useCancelAgentRun()

  const authErr = isAuthError(runs.error) ? runs.error : null
  if (authErr) {
    return <TokenGate error={authErr} onAuthed={() => runs.refetch()} />
  }

  const entries = runs.data?.entries ?? []
  const run = detail.data?.run as
    | { id: string; goal: string; status: string; principal: string; tier?: number; started_at: string; ended_at?: string; usage_json?: Usage; budget_json?: Record<string, number>; error?: string; result?: { answer?: string } }
    | undefined
  const steps = detail.data?.steps ?? []
  const running = run?.status === 'running'

  const submit = () => {
    const g = goal.trim()
    if (!g) return
    start.mutate(
      { goal: g, principal: 'admin', background: true },
      {
        onSuccess: (d) => {
          setGoal('')
          setSelected(d.run_id)
          runs.refetch()
        },
      },
    )
  }

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
            onKeyDown={(e) => { if (e.key === 'Enter') submit() }}
            disabled={start.isPending}
          />
          <button className="btn" disabled={!goal.trim() || start.isPending} onClick={submit}>
            {start.isPending ? '提交中…' : '开始'}
          </button>
        </div>
        <p className="text-[11px] text-muted">
          任务在后台运行，进度实时刷新，可随时取消；破坏性动作仍会停下来等人工确认。
        </p>
        {start.error && <ErrorBanner message={(start.error as Error).message} onRetry={() => start.reset()} />}
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
              <tr><th>开始</th><th>目标</th><th>触发者</th><th>状态</th><th>档位</th><th>耗时</th><th></th></tr>
            </thead>
            <tbody>
              {entries.map((r: AgentRun) => (
                <tr key={r.id} className="cursor-pointer" onClick={() => setSelected(r.id)}>
                  <td className="muted">{fmtTime(r.started_at)}</td>
                  <td className="raw" title={r.goal}>{r.goal}</td>
                  <td className="raw">{r.principal}</td>
                  <td>
                    <span className={`badge ${STATUS_TONE[r.status] || 'muted'}`}>
                      {r.status === 'running' && <Loader2 size={10} className="spin" />}
                      {STATUS_LABEL[r.status] || r.status}
                    </span>
                  </td>
                  <td className="muted">{r.tier ?? '-'}</td>
                  <td className="muted">{fmtDuration(r.started_at, r.ended_at)}</td>
                  <td>
                    <button className="btn ghost sm" onClick={(e) => { e.stopPropagation(); setSelected(r.id) }}>
                      {r.status === 'running' ? '查看进度' : '步骤'}
                    </button>
                  </td>
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
              <h3>任务详情</h3>
              <button className="btn ghost sm" onClick={() => setSelected(null)} aria-label="关闭">
                <XCircle size={14} />
              </button>
            </div>
            <div className="drawer-body space-y-3">
              {detail.isLoading && <Spinner label="加载任务…" />}
              {detail.error && <ErrorBanner message={(detail.error as Error).message} onRetry={() => detail.refetch()} />}

              {run && (
                <>
                  <div className="run-summary">
                    <div className="run-goal">{run.goal}</div>
                    <div className="run-facts">
                      <span className={`badge ${STATUS_TONE[run.status] || 'muted'}`}>
                        {run.status === 'running' && <Loader2 size={10} className="spin" />}
                        {STATUS_LABEL[run.status] || run.status}
                      </span>
                      <span className="badge muted">档位 {run.tier ?? '-'}</span>
                      <span className="badge muted">{fmtDuration(run.started_at, run.ended_at)}</span>
                      {run.usage_json && (
                        <span className="badge muted">
                          {run.usage_json.steps ?? 0} 步 · {run.usage_json.tool_calls ?? 0} 次工具
                          {run.usage_json.tokens ? ` · ${run.usage_json.tokens} tokens` : ''}
                        </span>
                      )}
                    </div>
                    {run.error && <div className="hint err">{run.error}</div>}
                    {run.status === 'done' && run.result?.answer && (
                      <div className="run-answer">{run.result.answer}</div>
                    )}
                    {running && (
                      <button
                        className="btn secondary sm"
                        onClick={() => cancel.mutate(run.id)}
                        disabled={cancel.isPending}
                      >
                        <Square size={11} /> {cancel.isPending ? '取消中…' : '取消任务'}
                      </button>
                    )}
                    {cancel.error && <ErrorBanner message={(cancel.error as Error).message} onRetry={() => cancel.reset()} />}
                  </div>

                  {steps.length === 0 && running && (
                    <div className="run-waiting"><Loader2 size={14} className="spin" /> Agent 正在规划第一步…</div>
                  )}
                  {steps.length === 0 && !running && <div className="empty">该任务没有记录步骤</div>}
                  {steps.map((s) => <StepCard key={s.id} step={s} />)}
                </>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
