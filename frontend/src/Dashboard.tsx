import { Link } from 'react-router-dom'
import { lazy, Suspense, useEffect, useRef, useState, type ReactNode } from 'react'
import type { EChartsOption } from './lib/echarts'
import {
  Inbox, MessageSquare, FileText, Users, BookOpen, ShieldAlert, Activity,
  Bot, Zap, Headset, Lock, RefreshCw, LayoutDashboard, CheckCircle2, AlertTriangle,
  Radio, ScrollText, MessagesSquare, BarChart3, QrCode, Loader2,
} from 'lucide-react'
import { QRCodeSVG } from 'qrcode.react'
import { http } from './lib/queries'
import { useAdminOverview, useAdminStats, useAdminReport, useAgentHealth, useAdminChannels, useConfig, useUpdateConfig, useFeedbackSummary, useChannelAdapters } from './lib/queries'
import { isAuthError } from './lib/queries'
import TokenGate from './components/TokenGate'
import type { ChannelStatus } from './types/api'
import { useAppStore } from './store/appStore'
import { StatCard, EmptyState, PageHead } from './ui'
import { CHANNEL_LABEL, ESCALATE_REASON, TOOL_LABEL, label } from './lib/labels'
import { Spinner, ErrorBanner } from './components/Spinner'
import './pages2.css'
import { t } from './lib/t'

const LazyEChart = lazy(() => import('./components/EChart'))

function EChart({ option, height = 260 }: { option: EChartsOption; height?: number }) {
  return (
    <Suspense fallback={<div role="status" style={{ width: '100%', height }}>{t('加载中…')}</div>}>
      <LazyEChart option={option} height={height} />
    </Suspense>
  )
}

// 图表配色跟随品牌：墨绿主色 / 朱砂 / 竹青 / 赭石 / 灰蓝，避免默认的 indigo 紫
const PALETTE = ['#0E5C55', '#17786C', '#C2452D', '#B98A4B', '#5E7C88', '#7FA99B', '#946B5C']
const AXIS = { axisLabel: { color: 'rgba(128,128,145,.85)', fontSize: 11 }, axisLine: { lineStyle: { color: 'rgba(128,128,145,.3)' } } }

export default function Dashboard() {
  const health = useAgentHealth()
  const overview = useAdminOverview()
  const stats = useAdminStats()
  const report = useAdminReport()
  const channelsQ = useAdminChannels()
  const feedbackSummary = useFeedbackSummary()
  const adapters = useChannelAdapters()
  const { data: cfgRes } = useConfig()
  const upd = useUpdateConfig()
  const adminToken = useAppStore((s) => s.adminToken)

  const ov = overview.data
  const st = stats.data
  const h = health.data
  const cfg = cfgRes?.config
  const chs = channelsQ.data?.channels
  // 401/403 表示令牌不对，503 表示服务端没配令牌——两种都要给登录/提示入口，不能白屏
  // 空数据判定：24 小时内没有任何消息
  const isEmpty = !!st && st.messages_by_hour.every((m) => !m.visitor && !m.agent && !m.operator)
  const authError = isAuthError(overview.error) ? overview.error
    : isAuthError(stats.error) ? stats.error
    : isAuthError(cfgRes === undefined ? health.error : undefined) ? health.error
    : null

  const refreshAll = () => {
    overview.refetch(); stats.refetch(); health.refetch(); channelsQ.refetch()
  }

  if (authError) {
    return <TokenGate error={authError} onAuthed={refreshAll} />
  }

  const setEngine = (mode: 'auto' | 'rule' | 'llm') => {
    if (!cfg) return
    const next = structuredClone(cfg)
    next.engine.mode = mode
    upd.mutate(next)
  }

  const hourOption: EChartsOption | undefined = st ? {
    tooltip: { trigger: 'axis' },
    legend: { data: [t('用户'), t('秘书'), t('管理员')], textStyle: { color: 'rgba(128,128,145,.9)', fontSize: 11 }, top: 0 },
    grid: { left: 36, right: 12, top: 30, bottom: 26 },
    xAxis: { type: 'category', data: st.messages_by_hour.map((m) => m.bucket), ...AXIS, axisLabel: { ...AXIS.axisLabel, formatter: (v: string) => v.slice(5).replace('T', ' ') } },
    yAxis: { type: 'value', ...AXIS, splitLine: { lineStyle: { color: 'rgba(128,128,145,.15)' } } },
    series: [
      { name: t('用户'), type: 'bar', stack: 'm', data: st.messages_by_hour.map((m) => m.visitor), itemStyle: { color: PALETTE[0] } },
      { name: t('秘书'), type: 'bar', stack: 'm', data: st.messages_by_hour.map((m) => m.agent), itemStyle: { color: PALETTE[1] } },
      { name: t('真人'), type: 'bar', stack: 'm', data: st.messages_by_hour.map((m) => m.operator), itemStyle: { color: PALETTE[2] } },
    ],
  } : undefined

  const dayOption: EChartsOption | undefined = st ? {
    tooltip: { trigger: 'axis' },
    grid: { left: 36, right: 12, top: 16, bottom: 26 },
    xAxis: { type: 'category', data: st.sessions_by_day.map((s) => s.day.slice(5)), ...AXIS },
    yAxis: { type: 'value', minInterval: 1, ...AXIS, splitLine: { lineStyle: { color: 'rgba(128,128,145,.15)' } } },
    series: [{
      name: t('新会话'), type: 'line', smooth: true, areaStyle: { opacity: 0.18 },
      data: st.sessions_by_day.map((s) => s.count), itemStyle: { color: PALETTE[4] },
    }],
  } : undefined

  const reportOption: EChartsOption | undefined = report.data ? {
    tooltip: { trigger: 'axis' },
    legend: { bottom: 0, textStyle: { color: 'rgba(128,128,145,.9)', fontSize: 11 } },
    grid: { left: 36, right: 40, top: 16, bottom: 46 },
    xAxis: { type: 'category', data: report.data.daily.map((d) => d.day.slice(5)), ...AXIS },
    yAxis: [
      { type: 'value', minInterval: 1, ...AXIS, splitLine: { lineStyle: { color: 'rgba(128,128,145,.15)' } } },
      { type: 'value', ...AXIS, axisLabel: { formatter: '{value}%' }, splitLine: { show: false } },
    ],
    series: [
      { name: t('会话'), type: 'bar', barMaxWidth: 14, data: report.data.daily.map((d) => d.sessions), itemStyle: { color: PALETTE[0] } },
      { name: t('机器人回复'), type: 'bar', barMaxWidth: 14, data: report.data.daily.map((d) => d.agent_replies), itemStyle: { color: PALETTE[4] } },
      { name: t('升级'), type: 'bar', barMaxWidth: 14, data: report.data.daily.map((d) => d.escalations), itemStyle: { color: PALETTE[3] } },
      {
        name: t('KB命中率'), type: 'line', smooth: true, yAxisIndex: 1,
        data: report.data.daily.map((d) => (d.agent_replies ? Math.round((d.kb_hits / d.agent_replies) * 100) : 0)),
        itemStyle: { color: PALETTE[1] },
      },
    ],
  } : undefined

  const channelOption: EChartsOption | undefined = st && st.channels.length > 0 ? {
    tooltip: { trigger: 'item' },
    legend: { bottom: 0, textStyle: { color: 'rgba(128,128,145,.9)', fontSize: 11 } },
    series: [{
      type: 'pie', radius: ['42%', '68%'], center: ['50%', '44%'],
      data: st.channels.map((c, i) => ({
        name: label(CHANNEL_LABEL, c.channel),
        value: c.count,
        itemStyle: { color: PALETTE[i % PALETTE.length] },
      })),
      label: { color: 'rgba(128,128,145,.9)', fontSize: 11, formatter: '{b} {c}' },
    }],
  } : undefined

  const feedbackOption: EChartsOption | undefined = st && (st.feedback.helpful > 0 || st.feedback.not_helpful > 0) ? {
    tooltip: { trigger: 'item' },
    legend: { bottom: 0, textStyle: { color: 'rgba(128,128,145,.9)', fontSize: 11 } },
    series: [{
      type: 'pie', radius: ['42%', '68%'], center: ['50%', '44%'],
      data: [
        { name: t('有帮助'), value: st.feedback.helpful, itemStyle: { color: PALETTE[1] } },
        { name: t('没帮助'), value: st.feedback.not_helpful, itemStyle: { color: PALETTE[3] } },
      ],
      label: { color: 'rgba(128,128,145,.9)', fontSize: 11, formatter: '{b} {c}' },
    }],
  } : undefined

  const escalateOption: EChartsOption | undefined = st && st.escalate_reasons.length > 0 ? {
    tooltip: { trigger: 'item' },
    legend: { type: 'scroll', bottom: 0, textStyle: { color: 'rgba(128,128,145,.9)', fontSize: 11 } },
    series: [{
      type: 'pie', radius: '62%', center: ['50%', '44%'], roseType: 'radius',
      data: st.escalate_reasons.map((e, i) => ({
        name: label(ESCALATE_REASON, e.reason),
        value: e.count,
        itemStyle: { color: PALETTE[i % PALETTE.length] },
      })),
      label: { color: 'rgba(128,128,145,.9)', fontSize: 11, formatter: '{b} {c}' },
    }],
  } : undefined

  const toolOption: EChartsOption | undefined = st && st.tool_usage.length > 0 ? {
    tooltip: { trigger: 'axis' },
    grid: { left: 110, right: 24, top: 10, bottom: 26 },
    xAxis: { type: 'value', minInterval: 1, ...AXIS, splitLine: { lineStyle: { color: 'rgba(128,128,145,.15)' } } },
    yAxis: { type: 'category', data: st.tool_usage.map((t) => label(TOOL_LABEL, t.tool)).reverse(), ...AXIS },
    series: [{
      name: t('调用次数'), type: 'bar',
      data: st.tool_usage.map((t) => t.count).reverse(), itemStyle: { color: PALETTE[5], borderRadius: [0, 6, 6, 0] },
    }],
  } : undefined

  const emptyOpt = {
    title: {
      text: t('暂无数据'), subtext: t('产生对话后这里会自动统计'),
      left: 'center', top: 'middle',
      textStyle: { color: 'rgba(128,128,145,.65)', fontSize: 13, fontWeight: 400 },
      subtextStyle: { color: 'rgba(128,128,145,.45)', fontSize: 11 },
    },
  }

  const funnelOption: EChartsOption | undefined = st ? {
    tooltip: { trigger: 'axis' },
    grid: { left: 80, right: 24, top: 10, bottom: 26 },
    xAxis: { type: 'value', minInterval: 1, ...AXIS, splitLine: { lineStyle: { color: 'rgba(128,128,145,.15)' } } },
    yAxis: { type: 'category', data: [t('消息'), t('KB回答'), t('工具调用'), t('升级'), t('拦截')], ...AXIS },
    series: [{
      name: t('近 24 小时'), type: 'bar', barWidth: 14,
      itemStyle: { color: PALETTE[4], borderRadius: [0, 6, 6, 0] },
      data: [st.funnel.messages, st.funnel.kb_answered, st.funnel.tool_calls, st.funnel.escalations, st.funnel.guardrail_blocked],
    }],
  } : undefined

  return (
    <div className="p-4 md:p-6 max-w-[1400px] mx-auto w-full space-y-4 pb-24 md:pb-10">
      <PageHead
        title={t('运行总览')}
        sub={t('会话、渠道与工作流的实时状态 · 30 秒自动刷新')}
        right={
          <>
            <Link to="/logs" className="btn ghost sm"><ScrollText size={13} /> {t('日志')}</Link>
            <Link to="/sessions" className="btn ghost sm"><MessagesSquare size={13} /> {t('会话')}</Link>
            {adminToken && (
              <span className="badge ok"><Lock size={11} /> {t('令牌已登录')}</span>
            )}
            {cfg && (
              <div className="flex items-center gap-1 bg-panel border border-line-2 rounded-full p-1">
                {(['auto', 'llm', 'rule'] as const).map((m) => (
                  <button
                    key={m}
                    disabled={upd.isPending}
                    onClick={() => setEngine(m)}
                    className={`px-3 py-1 rounded-full text-xs font-medium transition ${cfg.engine.mode === m ? 'bg-brand text-white shadow' : 'text-ink-2 hover:bg-panel-2'}`}
                  >
                    {m === 'auto' ? t('自动') : m === 'llm' ? 'LLM' : t('规则')}
                  </button>
                ))}
              </div>
            )}
          </>
        }
      />

      <div className="bg-panel border border-line-2 rounded-xl p-4 flex flex-wrap items-center gap-x-6 gap-y-2">
        <div className="flex items-center gap-2">
          <Bot size={16} className="text-brand" />
          <span className="font-semibold text-sm">{t('Agent 引擎')}</span>
          <span className={`badge ${h?.mode === 'llm_react' ? 'ok' : 'info'}`}>
            {h ? (h.mode === 'llm_react' ? `LLM · ${h.model}` : t('规则 + 检索')) : '…'}
          </span>
        </div>
        <HealthChip ok={!!h?.llm_ready} on={h?.llm_ready ? t('LLM Key 已配置') : t('未配 LLM Key（自动降级）')} />
        <HealthChip ok={true} on={`${t('知识库')} ${h?.kb_entries ?? '…'}`} />
        <HealthChip ok={true} on={`${t('工具')} ${h?.tool_count ?? '…'}`} />
        <HealthChip ok={true} on={`${t('护栏规则')} ${h?.injection_rules ?? '…'}`} />
        {/* 语音转写：没有它收语音消息只能回「听不了」，运维要能一眼看到 */}
        <HealthChip ok={h?.asr?.available !== false}
          on={h?.asr?.available === false ? t('语音转写未启用') : t('语音转写已启用')} />
        <Link to="/settings/params" className="text-xs text-brand hover:underline ml-auto">{t('进入管理中心 →')}</Link>
      </div>

      {ov && ov.pending > 0 && (
        <Link to="/operator" className="block bg-warn-soft border border-warn rounded-xl p-4 flex items-center gap-3 hover:opacity-90 transition">
          <AlertTriangle size={18} className="text-warn" />
          <span className="text-sm font-medium">{t('有')} {ov.pending} {t('个会话等待接续')}</span>
          <span className="btn sm ml-auto"><Headset size={13} /> {t('前往协作台')}</span>
        </Link>
      )}

      {overview.isLoading ? (
        <Spinner full label={t('加载总览…')} />
      ) : overview.error ? (
        <ErrorBanner message={(overview.error as Error).message} onRetry={() => overview.refetch()} />
      ) : !ov ? (
        <EmptyState icon={<Inbox size={22} />} title={t('暂无数据')} desc={t('还没有会话记录，接入渠道后这里会出现统计。')} />
      ) : (
        <div className="stat-grid">
          <StatCard icon={<Inbox size={18} />} value={ov.sessions} label={t('总会话')} tone="brand" />
          <StatCard icon={<MessageSquare size={18} />} value={ov.messages} label={t('消息量')} tone="ok" />
          <StatCard icon={<FileText size={18} />} value={ov.tickets} label={t('工单')} tone="warn" />
          <StatCard icon={<Users size={18} />} value={ov.pending} label={t('待接管')} tone="danger" />
          <StatCard icon={<BookOpen size={18} />} value={ov.kb_count} label={t('知识库')} tone="brand" />
          <StatCard icon={<ShieldAlert size={18} />} value={ov.injection} label={t('注入拦截')} tone="danger" />
          <StatCard icon={<CheckCircle2 size={18} />} value={`${(ov.feedback.help_rate * 100).toFixed(0)}%`} label={t('好评率')} tone="ok" />
        </div>
      )}

      {stats.error && !isAuthError(stats.error) && <ErrorBanner message={(stats.error as Error).message} onRetry={() => stats.refetch()} />}

      {/* 空数据时不要渲染 8 个空图表：占满整屏且全是"暂无数据"，用户看不到有用信息。
          改成一张引导卡 + 渠道连接状态（后者即使无对话也有意义）。 */}
      {st && isEmpty && (
        <div className="grid gap-4 md:grid-cols-3">
          <div className="md:col-span-2 bg-panel border border-line-2 rounded-xl p-8 flex flex-col items-center justify-center text-center" style={{ minHeight: 280 }}>
            <div className="w-12 h-12 rounded-xl grid place-items-center mb-4" style={{ background: 'var(--brand-soft)', color: 'var(--brand)' }}>
              <MessageSquare size={22} />
            </div>
            <h3 className="text-base font-semibold mb-1">{t('还没有对话数据')}</h3>
            <p className="text-xs text-muted mb-4" style={{ maxWidth: 380, lineHeight: 1.8 }}>
              发一条测试消息，图表就会实时亮起来。<br />
              QQ 渠道需要先启动 NapCat 与网关。
            </p>
            <div className="flex gap-2">
              <Link to="/" className="btn sm">{t('打开对话页')}</Link>
              <Link to="/settings/params" className="btn secondary sm">{t('配置模型')}</Link>
            </div>
          </div>
          <ChartCard title={t('渠道连接')} icon={<Radio size={15} />}>
            {chs && chs.length > 0 ? (
              <div className="flex flex-col justify-center gap-4 px-2" style={{ height: 240 }}>
                {chs.map((c) => {
                  const adapter = c.channel === 'onebot_v11' ? adapters.data?.adapters?.[0] : undefined
                  const caps = adapter ? Object.entries(adapter.supports).filter(([, ok]) => ok).map(([k]) => k) : []
                  return (
                  <div key={c.channel} className="flex items-center gap-3 text-sm flex-wrap">
                    <span className={`w-2.5 h-2.5 rounded-full shrink-0 ${c.status === 'online' ? 'bg-ok' : c.status === 'stale' ? 'bg-warn' : c.status === 'builtin' ? 'bg-brand' : 'bg-danger'}`} />
                    <span className="font-medium w-[110px]">{label(CHANNEL_LABEL, c.channel)}</span>
                    <span className="text-xs text-muted">{channelText(c)}</span>
                    {caps.length > 0 && (
                      <span className="flex gap-1 flex-wrap">
                        {caps.map((cap) => <span key={cap} className="badge muted" style={{ fontSize: 10 }}>{cap}</span>)}
                      </span>
                    )}
                    {c.channel === 'onebot_v11' && <QQScanLogin onLoggedIn={refreshAll} />}
                  </div>
                  )
                })}
              </div>
            ) : (
              <div className="h-[240px] grid place-items-center text-xs text-muted">{t('加载中…')}</div>
            )}
          </ChartCard>
        </div>
      )}

      {st && !isEmpty && (
        <div className="grid gap-4 md:grid-cols-2">
          <ChartCard title={t('近 24 小时消息量')} icon={<MessageSquare size={15} />}>
            <EChart option={hourOption ?? emptyOpt} height={250} />
          </ChartCard>
          <ChartCard title={t('近 7 天新会话')} icon={<Activity size={15} />}>
            <EChart option={dayOption ?? emptyOpt} height={250} />
          </ChartCard>
          <ChartCard title={t('经营报表 · 近 7 天（会话/回复/升级 + KB 命中率）')} icon={<BarChart3 size={15} />}>
            <EChart option={reportOption ?? emptyOpt} height={250} />
          </ChartCard>
          <ChartCard title={t('渠道分布')} icon={<Bot size={15} />}>
            <EChart option={channelOption ?? emptyOpt} height={250} />
          </ChartCard>
          <ChartCard title={t('升级原因')} icon={<Headset size={15} />}>
            <EChart option={escalateOption ?? emptyOpt} height={250} />
          </ChartCard>
          <ChartCard title={t('工具调用排行')} icon={<Zap size={15} />}>
            <EChart option={toolOption ?? emptyOpt} height={250} />
          </ChartCard>
          <ChartCard title={t('反馈构成')} icon={<CheckCircle2 size={15} />}>
            {feedbackOption ? (
              <EChart option={feedbackOption} height={250} />
            ) : (
              <div className="h-[250px] flex flex-col items-center justify-center gap-2 text-center px-6">
                <div className="w-9 h-9 rounded-lg grid place-items-center" style={{ background: 'var(--ok-soft)', color: '#1a8f52' }}>
                  <CheckCircle2 size={17} />
                </div>
                <div className="text-xs text-muted" style={{ lineHeight: 1.8 }}>
                  还没有用户评价<br />回答下方的「有用 / 没用」会汇总到这里
                </div>
              </div>
            )}
          </ChartCard>
          <ChartCard title={t('渠道连接')} icon={<Radio size={15} />}>
            {chs && chs.length > 0 ? (
              <div className="h-[250px] flex flex-col justify-center gap-4 px-2">
                {chs.map((c) => {
                  const adapter = c.channel === 'onebot_v11' ? adapters.data?.adapters?.[0] : undefined
                  const caps = adapter ? Object.entries(adapter.supports).filter(([, ok]) => ok).map(([k]) => k) : []
                  return (
                  <div key={c.channel} className="flex items-center gap-3 text-sm flex-wrap">
                    <span className={`w-2.5 h-2.5 rounded-full shrink-0 ${c.status === 'online' ? 'bg-ok' : c.status === 'stale' ? 'bg-warn' : c.status === 'builtin' ? 'bg-brand' : 'bg-danger'}`} />
                    <span className="font-medium w-[110px]">{label(CHANNEL_LABEL, c.channel)}</span>
                    <span className="text-xs text-muted">{channelText(c)}</span>
                    {caps.length > 0 && (
                      <span className="flex gap-1 flex-wrap">
                        {caps.map((cap) => <span key={cap} className="badge muted" style={{ fontSize: 10 }}>{cap}</span>)}
                      </span>
                    )}
                    {c.channel === 'onebot_v11' && <QQScanLogin onLoggedIn={refreshAll} />}
                  </div>
                  )
                })}
                <p className="text-[11px] text-muted">{t('QQ 网关离线？启动 NapCat(:3001) 与 gateway/bot.py，每 30 秒自动心跳上报。')}</p>
              </div>
            ) : (
              <EChart option={emptyOpt} height={250} />
            )}
          </ChartCard>
          <ChartCard title={t('工作流漏斗（近 24 小时）')} icon={<Activity size={15} />}>
            <EChart option={funnelOption ?? emptyOpt} height={250} />
          </ChartCard>
        </div>
      )}


    </div>
  )
}

function HealthChip({ ok, on }: { ok: boolean; on: string }) {
  return (
    <span className={`badge ${ok ? 'muted' : 'warn'} text-[11px]`}>{on}</span>
  )
}

/* ============ QQ 扫码登录：经核心代理 NapCat WebUI，token 只留在服务端 ============ */
interface QQLoginStatus {
  configured: boolean
  reachable?: boolean
  is_login?: boolean
  is_offline?: boolean
  core_ready?: boolean
  login_phase?: number | null
  uin?: string
  nick?: string
  error?: string
}

const QR_TTL_S = 110 // NapCat 二维码约 2 分钟过期，提前 10 秒主动换新

function QQScanLogin({ onLoggedIn }: { onLoggedIn?: () => void }) {
  const [open, setOpen] = useState(false)
  const [qr, setQr] = useState<{ url?: string; error?: string } | null>(null)
  const [status, setStatus] = useState<QQLoginStatus | null>(null)
  const [busy, setBusy] = useState(false)
  const secsRef = useRef(0)
  const openRef = useRef(false)
  openRef.current = open

  const loadQr = async (refresh: boolean) => {
    try {
      const r = await http.get<{ configured: boolean; qrcode_url?: string; error?: string }>(
        `/api/channel/qq/qr${refresh ? '?refresh=1' : ''}`)
      setQr({ url: r.qrcode_url, error: r.error })
      secsRef.current = 0
    } catch (e) {
      setQr({ error: (e as Error).message })
    }
  }

  // 打开弹窗：先查状态（可能已经登录着），未登录才拉二维码 + 启动轮询
  const openDialog = () => {
    setOpen(true)
    setBusy(true)
    http.get<QQLoginStatus>('/api/channel/qq/login-status')
      .then((s) => { if (openRef.current) setStatus(s) })
      .catch((e) => { if (openRef.current) setStatus({ configured: true, reachable: false, error: (e as Error).message }) })
      .finally(() => setBusy(false))
    if (!status?.is_login) void loadQr(false)
  }

  // 1s 计数（二维码到期自动换新）+ 3s 轮询登录状态，弹窗关了全部停
  useEffect(() => {
    if (!open) return
    const tick = setInterval(() => {
      secsRef.current += 1
      if (qr?.url && secsRef.current >= QR_TTL_S) void loadQr(true)
    }, 1000)
    const poll = setInterval(() => {
      http.get<QQLoginStatus>('/api/channel/qq/login-status')
        .then((s) => { if (openRef.current) setStatus(s) })
        .catch(() => { /* 网络抖动等下一轮 */ })
    }, 3000)
    return () => { clearInterval(tick); clearInterval(poll) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, qr?.url])

  const loggedIn = !!status?.is_login
  const close = () => { setOpen(false); setQr(null); if (loggedIn) onLoggedIn?.() }

  return (
    <>
      <button
        className="btn secondary sm"
        style={{ marginLeft: 'auto', fontSize: 11, padding: '3px 10px' }}
        onClick={(e) => { e.stopPropagation(); openDialog() }}
        title={t('不出控制台直接扫码登录 QQ（NapCat WebUI 令牌由服务端保管）')}
      >
        <QrCode size={12} /> {t('扫码登录')}
      </button>
      {open && (
        <div className="dlg-mask" onClick={close}>
          <div className="dlg" style={{ width: 'min(380px, 100%)' }} onClick={(e) => e.stopPropagation()}>
            <div className="dlg-title" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <span>{t('QQ 扫码登录')}</span>
              <button className="btn ghost sm" onClick={close}>{t('关闭')}</button>
            </div>

            {busy && <div className="bp-hint"><Loader2 size={13} className="animate-spin inline-block" /> {t('连接 NapCat…')}</div>}

            {loggedIn && (
              <div style={{ textAlign: 'center', padding: '10px 0' }}>
                <CheckCircle2 size={40} style={{ color: 'var(--ok, #16a34a)' }} />
                <p className="dlg-msg" style={{ marginTop: 8 }}>
                  {t('已登录')}{status?.nick || status?.uin ? `：${status?.nick || ''}${status?.uin ? ` (${status?.uin})` : ''}` : ''}
                </p>
                <p className="bp-hint">{t('网关会自动连上 NapCat，无需重启；回到「渠道连接」看心跳。')}</p>
              </div>
            )}

            {!busy && !loggedIn && status && status.configured === false && (
              <div>
                <p className="dlg-msg">{t('还没有配置 NapCat WebUI 令牌，控制台拿不到二维码。')}</p>
                <p className="bp-hint">
                  {t('在 .env 里设置 NAPCAT_WEBUI_TOKEN（NapCat 启动日志里 grep token，或 docs/napcat-compose.yaml 的 NAPCAT_WEBUI_TOKEN），重启核心后再来。')}
                </p>
              </div>
            )}

            {!busy && !loggedIn && status && status.configured === true && status.reachable === false && (
              <div>
                <p className="dlg-msg">{t('连不上 NapCat WebUI（默认 http://127.0.0.1:6099）。')}</p>
                <p className="bp-hint">{status.error}{t('；确认 NapCat 在跑、6099 端口可达、令牌正确。')}</p>
              </div>
            )}

            {!busy && !loggedIn && qr?.url && (
              <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 10 }}>
                <div style={{ background: '#fff', padding: 12, borderRadius: 10, lineHeight: 0 }}>
                  <QRCodeSVG value={qr.url} size={200} />
                </div>
                <p className="bp-hint" style={{ marginTop: 0 }}>
                  {t('用手机 QQ「扫一扫」扫这个码，扫码后这里会自动变成已登录。')}
                </p>
                <button className="btn secondary sm" onClick={() => void loadQr(true)}>
                  <RefreshCw size={12} /> {t('刷新二维码')}
                </button>
              </div>
            )}

            {!busy && !loggedIn && qr?.error && status?.reachable !== false && (
              <div>
                <p className="dlg-msg">{qr.error}</p>
                <p className="bp-hint">{t('拿不到二维码时，仍可用老办法：打开 NapCat WebUI（http://127.0.0.1:6099）或运行 scripts/qq_login.py。')}</p>
              </div>
            )}
          </div>
        </div>
      )}
    </>
  )
}

function channelText(c: ChannelStatus): string {
  const ago = c.last_seen ? `${Math.max(0, Math.round(Date.now() / 1000 - c.last_seen))} 秒前心跳` : ''
  if (c.status === 'online') return `在线 · ${ago}`
  if (c.status === 'stale') return '心跳超时（>90 秒未上报）'
  if (c.status === 'builtin') return '内置可用'
  return '离线 · 等待网关接入'
}

function ChartCard({ title, icon, children }: { title: string; icon: ReactNode; children: ReactNode }) {
  return (
    <div className="bg-panel border border-line-2 rounded-xl p-4">
      <div className="flex items-center gap-2 mb-1">
        <span className="text-brand">{icon}</span>
        <h3 className="text-sm font-semibold">{title}</h3>
      </div>
      {children}
    </div>
  )
}
