import { useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { BarChart3, Brain, Activity, KeyRound, ChevronLeft, ChevronRight, ShieldAlert, MessageSquare, FileText, Users, Inbox, BookOpen } from 'lucide-react'
import { useAdminOverview, useAdminReport, useAdminStats, useAdminSchema, useAdminPurge, useAdminMemory, useAdminInjection } from '../lib/queries'
import { usePagination } from '../lib/pagination'
import { StatCard, EmptyState } from '../ui'
import { Spinner, ErrorBanner } from '../components/Spinner'
import type { MemoryEntry, InjectionLogEntry } from '../types/api'
import { t as tr } from '../lib/t'
import { TOOL_LABEL, label } from '../lib/labels'
import { Check } from 'lucide-react'
import { RepItem, Pager } from './bits'
import { useFeedback } from '../components/Feedback'
import { useAppStore } from '../store/appStore'

export function DataPanel() {
  const { data: ov, isLoading } = useAdminOverview()
  const { data: report } = useAdminReport()
  const { data: stats } = useAdminStats()
  const { data: schema } = useAdminSchema()
  const purge = useAdminPurge()
  const fb = useFeedback()
  const [confirmPurge, setConfirmPurge] = useState(false)
  if (isLoading || !ov) return <Spinner full label={tr('加载数据…')} />

  const days = report?.daily ?? []
  // 汇总口径统一走后端 totals：升级率分母是会话数（不是回复数），前后端各算一套必然对不上
  const totalSessions = report?.totals.sessions ?? 0
  const totalReplies = report?.totals.agent_replies ?? 0
  const totalKb = report?.totals.kb_hits ?? 0
  const totalEsc = report?.totals.escalations ?? 0
  const kbRate = report?.totals.kb_hit_rate != null ? Math.round(report.totals.kb_hit_rate * 100) : 0
  const escRate = report?.totals.escalate_rate != null ? Math.round(report.totals.escalate_rate * 100) : 0
  const dayMax = Math.max(1, ...days.map((x) => Math.max(x.sessions, x.agent_replies)))
  const toolMax = Math.max(1, ...(stats?.tool_usage ?? []).map((x) => x.count))

  return (
    <>
      <div className="stat-grid">
        <StatCard icon={<Inbox size={18} />} value={ov.sessions} label={tr('会话')} tone="brand" />
        <StatCard icon={<MessageSquare size={18} />} value={ov.messages} label={tr('消息')} tone="ok" />
        <StatCard icon={<FileText size={18} />} value={ov.tickets} label={tr('工单')} tone="warn" />
        <StatCard icon={<Users size={18} />} value={ov.pending} label={tr('待接管')} tone="danger" />
        <StatCard icon={<BookOpen size={18} />} value={ov.kb_count} label={tr('知识库')} tone="brand" />
        <StatCard icon={<ShieldAlert size={18} />} value={ov.injection} label={tr('注入拦截')} tone="danger" />
        <StatCard icon={<Activity size={18} />} value={ov.feedback.total} label={`反馈 · 好评率 ${(ov.feedback.help_rate * 100).toFixed(1)}%`} tone="ok" />
      </div>

      {/* 诊断区：DB 版本 / 表容量 / 数据清理（/api/admin/schema + /api/admin/purge） */}
      <div className="grp" style={{ marginTop: 14 }}>
        <div className="grp-head">
          <h3>{tr('存储与维护')}</h3>
          <span className="gh-note">{schema ? tr('schema v{v}', { v: schema.schema_version }) : tr('…')}</span>
        </div>
        <div className="grp-body">
          <div className="flex flex-wrap gap-1.5 mb-3">
            {Object.entries(schema?.tables ?? {}).map(([t, n]) => (
              <span key={t} className="badge muted" title={`${t}: ${n} 行`}>{t} · {n}</span>
            ))}
          </div>
          {confirmPurge ? (
            <div className="flex items-center gap-2 flex-wrap text-xs">
              <span style={{ color: 'var(--danger)' }}>{tr('将清理 180 天前的运营数据（审计链保留），确定？')}</span>
              <button className="btn sm" style={{ background: 'var(--danger)', color: '#fff' }}
                disabled={purge.isPending}
                onClick={() => purge.mutate(180, {
                  onSuccess: (r) => {
                    setConfirmPurge(false)
                    fb.toast.success(tr('已清理 {n} 条', { n: Object.values(r.removed || {}).reduce((a: number, b) => a + Number(b || 0), 0) }))
                  },
                  onError: (e) => fb.toast.error(`${tr('清理失败')}: ${(e as Error).message}`),
                })}>
                {purge.isPending ? tr('清理中…') : tr('确认清理')}
              </button>
              <button className="btn ghost sm" onClick={() => setConfirmPurge(false)}>{tr('取消')}</button>
            </div>
          ) : (
            <button className="btn secondary sm" onClick={() => setConfirmPurge(true)}>{tr('清理 180 天前的运营数据…')}</button>
          )}
        </div>
      </div>

      <div className="grp" style={{ marginTop: 14 }}>
        <div className="grp-head">
          <h3>{tr('近 7 天经营指标')}</h3>
          <span className="gh-note">{tr('按日聚合')}</span>
        </div>
        <div className="grp-body">
          <div className="rep-grid">
            <RepItem label={tr('新增会话')} value={totalSessions} sub={tr('7 天累计')} />
            <RepItem label={tr('Agent 回复')} value={totalReplies} sub={tr('7 天累计')} />
            <RepItem
              label={tr('KB 命中率')}
              value={`${kbRate}%`}
              sub={tr('KB引用统计', { n: totalKb })}
              tone={kbRate >= 60 ? 'ok' : kbRate > 0 ? 'warn' : 'muted'}
            />
            <RepItem
              label={tr('升级率')}
              value={`${escRate}%`}
              sub={tr('升级统计', { n: totalEsc })}
              tone={escRate > 40 ? 'warn' : 'ok'}
            />
          </div>
          {days.length > 0 && (
            <>
              <div className="rep-bars">
                {days.map((d) => (
                  <div
                    className="rep-day"
                    key={d.day}
                    title={`${d.day}：会话 ${d.sessions} · 回复 ${d.agent_replies} · 升级 ${d.escalations}`}
                  >
                    <div className="rep-bar-pair">
                      <span className="rep-bar sessions" style={{ height: `${(d.sessions / dayMax) * 100}%` }} />
                      <span className="rep-bar replies" style={{ height: `${(d.agent_replies / dayMax) * 100}%` }} />
                    </div>
                    <span className="rep-day-label">{d.day.slice(5)}</span>
                  </div>
                ))}
              </div>
              <div className="rep-legend">
                <span><i className="dot sessions" /> {tr('新会话')}</span>
                <span><i className="dot replies" /> {tr('Agent 回复')}</span>
              </div>
            </>
          )}
        </div>
      </div>

      {stats && stats.tool_usage.length > 0 && (
        <div className="grp" style={{ marginTop: 14 }}>
          <div className="grp-head">
            <h3>{tr('工具调用分布')}</h3>
            <span className="gh-note">{tr('近 24 小时')}</span>
          </div>
          <div className="grp-body">
            <div className="tool-dist">
              {stats.tool_usage.map((t) => (
                <div className="tool-dist-row" key={t.tool}>
                  <span className="tool-dist-name">{TOOL_LABEL[t.tool] || t.tool}</span>
                  <span className="tool-dist-track">
                    <span style={{ width: `${(t.count / toolMax) * 100}%` }} />
                  </span>
                  <span className="tool-dist-count">{t.count}</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </>
  )
}


export function MemPanel() {
  const { data: memRes, isLoading } = useAdminMemory()
  const mem: MemoryEntry[] = memRes?.entries ?? []
  // usePagination 必须在任何 return 之前调用：否则"加载中 → 有数据"时
  // hook 数量变化，React 直接抛 #310 并整页白屏（已复现）。
  const pg = usePagination(mem)
  if (isLoading) return <Spinner full label={tr('加载记忆…')} />
  if (!mem.length) return <EmptyState icon={<Brain size={22} />} title={tr('暂无长期记忆')} desc={tr('用户产生偏好或多次对话后会自动沉淀在这里。')} />
  return (
    <>
      <div className="tbl-wrap">
        <table className="dt">
          <thead><tr><th>{tr('用户')}</th><th>Key</th><th>Value</th><th>{tr('更新')}</th></tr></thead>
          <tbody>
            {pg.pageItems.map((m: MemoryEntry, i: number) => (
              <tr key={`${m.visitor_id}-${m.key}-${i}`}>
                <td>{m.visitor_id}</td>
                <td><span className="badge info">{m.key}</span></td>
                <td>{m.value}</td>
                <td className="muted">{m.updated_at?.slice(0, 16)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {pg.hasPages && <Pager page={pg.page} maxPage={pg.maxPage} total={pg.total} onChange={pg.setPage} label={tr('长期记忆')} />}
    </>
  )
}

/* ============ 注入日志 ============ */
export function InjPanel() {
  const { data: injRes, isLoading } = useAdminInjection()
  const inj: InjectionLogEntry[] = injRes?.entries ?? []
  // 同 MemPanel：hooks 必须在早退之前
  const pg = usePagination(inj)
  if (isLoading) return <Spinner full label={tr('加载注入日志…')} />
  if (!inj.length) return <EmptyState icon={<ShieldAlert size={22} />} title={tr('暂无注入拦截记录')} desc={tr('护栏被触发后会出现在这里。')} />
  return (
    <>
      <div className="tbl-wrap">
        <table className="dt">
          <thead><tr><th>#</th><th>{tr('用户')}</th><th>{tr('原始输入')}</th><th>{tr('命中')}</th><th>{tr('动作')}</th></tr></thead>
          <tbody>
            {pg.pageItems.map((m: InjectionLogEntry) => (
              <tr key={m.id}>
                <td>{m.id}</td>
                <td>{m.visitor_id}</td>
                <td className="raw" title={m.raw_input}>{m.raw_input}</td>
                <td><span className="badge danger">{m.detected}</span></td>
                <td>{m.action}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {pg.hasPages && <Pager page={pg.page} maxPage={pg.maxPage} total={pg.total} onChange={pg.setPage} label={tr('注入日志')} />}
    </>
  )
}


/* ============ 档案（人物/群）：后端 /api/profiles ============ */

export function TokenPanel() {
  const adminToken = useAppStore((s) => s.adminToken)
  const setAdminToken = useAppStore((s) => s.setAdminToken)
  const [copied, setCopied] = useState(false)
  const [state, setState] = useState<'idle' | 'loading' | 'ok' | 'fail'>('idle')
  const [msg, setMsg] = useState('')

  const fetchToken = async () => {
    setState('loading')
    try {
      const r = await fetch('/api/bootstrap/local-token')
      const d = await r.json().catch(() => ({}))
      if (r.ok && d.available && d.token) {
        setAdminToken(d.token)
        setState('ok'); setMsg('已从本机服务获取到令牌')
      } else {
        setState('fail'); setMsg(d.detail || d.reason || `HTTP ${r.status}`)
      }
    } catch (e) {
      setState('fail'); setMsg((e as Error).message)
    }
  }

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(adminToken)
      setCopied(true); setTimeout(() => setCopied(false), 1800)
    } catch { setMsg('复制被浏览器拒绝，请手动选中') }
  }

  return (
    <div className="space-y-4">
      <div className="text-sm text-muted" style={{ lineHeight: 1.8 }}>
        控制台用的是「访问令牌」而不是账号密码。本机打开时自动获取，远程访问才需要手动粘贴。
      </div>

      <div className="card p-4 space-y-3">
        <div className="flex items-center gap-2 text-sm font-semibold"><KeyRound size={15} /> {tr('当前令牌')}</div>
        {adminToken ? (
          <>
            <button
              className="w-full text-left font-mono text-xs border border-line-2 rounded-lg p-3 break-all"
              onClick={copy}
              title={tr('点击复制')}
            >
              {copied ? <span className="text-ok"><Check size={12} /> {tr('已复制到剪贴板')}</span> : adminToken}
            </button>
            <div className="flex gap-2">
              <button className="btn sm" onClick={copy}>{copied ? '已复制' : '复制令牌'}</button>
              <button className="btn secondary sm" onClick={fetchToken}>{tr('重新获取')}</button>
              <button className="btn ghost sm" onClick={() => { setAdminToken(''); setMsg('已清除，刷新页面后需重新登录') }}>
                清除
              </button>
            </div>
          </>
        ) : (
          <div className="text-xs text-muted">{tr('本机还没有令牌。点下面的按钮从服务端获取。')}</div>
        )}
        {msg && <div className="text-[11px] text-muted">{msg}</div>}
      </div>

      <div className="card p-4 space-y-2">
        <div className="text-sm font-semibold">{tr('换一台设备访问？')}</div>
        <div className="text-xs text-muted" style={{ lineHeight: 1.8 }}>
          1. 在这台机器上点「复制令牌」；<br />
          2. 在另一台设备打开控制台，粘贴到登录框；<br />
          3. 令牌只存在浏览器本地，不会上传。
        </div>
      </div>

      <div className="card p-4 space-y-2">
        <div className="text-sm font-semibold">{tr('安全说明')}</div>
        <div className="text-xs text-muted" style={{ lineHeight: 1.8 }}>
          本机自动获取只在 <code>127.0.0.1</code> / <code>::1</code> 回环地址生效，公网来源一律 403。
          公网部署建议设 <code>CS_AGENT_LOCAL_TOKEN_BOOTSTRAP=0</code> 彻底关闭该通道。
        </div>
      </div>
    </div>
  )
}
