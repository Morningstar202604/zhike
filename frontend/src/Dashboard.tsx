import { Link } from 'react-router-dom'
import { useState, type ReactNode } from 'react'
import type { EChartsOption } from 'echarts'
import {
  Inbox, MessageSquare, FileText, Users, BookOpen, ShieldAlert, Activity,
  Bot, Zap, Headset, Lock, RefreshCw, LayoutDashboard, CheckCircle2, AlertTriangle,
  Radio, ScrollText, MessagesSquare,
} from 'lucide-react'
import { useAdminOverview, useAdminStats, useAgentHealth, useAdminChannels, useConfig, useUpdateConfig } from './lib/queries'
import type { ChannelStatus } from './types/api'
import { useAppStore } from './store/appStore'
import { StatCard, EmptyState, PageHead } from './ui'
import { Spinner, ErrorBanner } from './components/Spinner'
import EChart from './components/EChart'
import './pages2.css'

const PALETTE = ['#6366f1', '#22c55e', '#f59e0b', '#ef4444', '#06b6d4', '#a855f7', '#ec4899']
const AXIS = { axisLabel: { color: 'rgba(128,128,145,.85)', fontSize: 11 }, axisLine: { lineStyle: { color: 'rgba(128,128,145,.3)' } } }

export default function Dashboard() {
  const health = useAgentHealth()
  const overview = useAdminOverview()
  const stats = useAdminStats()
  const channelsQ = useAdminChannels()
  const { data: cfgRes } = useConfig()
  const upd = useUpdateConfig()
  const adminToken = useAppStore((s) => s.adminToken)
  const setAdminToken = useAppStore((s) => s.setAdminToken)
  const [tokenInput, setTokenInput] = useState('')

  const ov = overview.data
  const st = stats.data
  const h = health.data
  const cfg = cfgRes?.config
  const chs = channelsQ.data?.channels
  const needsToken = /令牌/.test((overview.error as Error | null)?.message || '') || /令牌/.test((stats.error as Error | null)?.message || '')

  const setEngine = (mode: 'auto' | 'rule' | 'llm') => {
    if (!cfg) return
    const next = structuredClone(cfg)
    next.engine.mode = mode
    upd.mutate(next)
  }

  const hourOption: EChartsOption | undefined = st ? {
    tooltip: { trigger: 'axis' },
    legend: { data: ['访客', 'Agent', '坐席'], textStyle: { color: 'rgba(128,128,145,.9)', fontSize: 11 }, top: 0 },
    grid: { left: 36, right: 12, top: 30, bottom: 26 },
    xAxis: { type: 'category', data: st.messages_by_hour.map((m) => m.bucket), ...AXIS, axisLabel: { ...AXIS.axisLabel, formatter: (v: string) => v.slice(5).replace('T', ' ') } },
    yAxis: { type: 'value', ...AXIS, splitLine: { lineStyle: { color: 'rgba(128,128,145,.15)' } } },
    series: [
      { name: '访客', type: 'bar', stack: 'm', data: st.messages_by_hour.map((m) => m.visitor), itemStyle: { color: PALETTE[0] } },
      { name: 'Agent', type: 'bar', stack: 'm', data: st.messages_by_hour.map((m) => m.agent), itemStyle: { color: PALETTE[1] } },
      { name: '坐席', type: 'bar', stack: 'm', data: st.messages_by_hour.map((m) => m.operator), itemStyle: { color: PALETTE[2] } },
    ],
  } : undefined

  const dayOption: EChartsOption | undefined = st ? {
    tooltip: { trigger: 'axis' },
    grid: { left: 36, right: 12, top: 16, bottom: 26 },
    xAxis: { type: 'category', data: st.sessions_by_day.map((s) => s.day.slice(5)), ...AXIS },
    yAxis: { type: 'value', minInterval: 1, ...AXIS, splitLine: { lineStyle: { color: 'rgba(128,128,145,.15)' } } },
    series: [{
      name: '新会话', type: 'line', smooth: true, areaStyle: { opacity: 0.18 },
      data: st.sessions_by_day.map((s) => s.count), itemStyle: { color: PALETTE[4] },
    }],
  } : undefined

  const channelOption: EChartsOption | undefined = st && st.channels.length > 0 ? {
    tooltip: { trigger: 'item' },
    legend: { bottom: 0, textStyle: { color: 'rgba(128,128,145,.9)', fontSize: 11 } },
    series: [{
      type: 'pie', radius: ['42%', '68%'], center: ['50%', '44%'],
      data: st.channels.map((c, i) => ({ name: c.channel, value: c.count, itemStyle: { color: PALETTE[i % PALETTE.length] } })),
      label: { color: 'rgba(128,128,145,.9)', fontSize: 11, formatter: '{b} {c}' },
    }],
  } : undefined

  const feedbackOption: EChartsOption | undefined = st && (st.feedback.helpful > 0 || st.feedback.not_helpful > 0) ? {
    tooltip: { trigger: 'item' },
    legend: { bottom: 0, textStyle: { color: 'rgba(128,128,145,.9)', fontSize: 11 } },
    series: [{
      type: 'pie', radius: ['42%', '68%'], center: ['50%', '44%'],
      data: [
        { name: '有帮助', value: st.feedback.helpful, itemStyle: { color: PALETTE[1] } },
        { name: '没帮助', value: st.feedback.not_helpful, itemStyle: { color: PALETTE[3] } },
      ],
      label: { color: 'rgba(128,128,145,.9)', fontSize: 11, formatter: '{b} {c}' },
    }],
  } : undefined

  const escalateOption: EChartsOption | undefined = st && st.escalate_reasons.length > 0 ? {
    tooltip: { trigger: 'item' },
    legend: { type: 'scroll', bottom: 0, textStyle: { color: 'rgba(128,128,145,.9)', fontSize: 11 } },
    series: [{
      type: 'pie', radius: '62%', center: ['50%', '44%'], roseType: 'radius',
      data: st.escalate_reasons.map((e, i) => ({ name: e.reason, value: e.count, itemStyle: { color: PALETTE[i % PALETTE.length] } })),
      label: { color: 'rgba(128,128,145,.9)', fontSize: 11, formatter: '{b} {c}' },
    }],
  } : undefined

  const toolOption: EChartsOption | undefined = st && st.tool_usage.length > 0 ? {
    tooltip: { trigger: 'axis' },
    grid: { left: 110, right: 24, top: 10, bottom: 26 },
    xAxis: { type: 'value', minInterval: 1, ...AXIS, splitLine: { lineStyle: { color: 'rgba(128,128,145,.15)' } } },
    yAxis: { type: 'category', data: st.tool_usage.map((t) => t.tool).reverse(), ...AXIS },
    series: [{
      name: '调用次数', type: 'bar',
      data: st.tool_usage.map((t) => t.count).reverse(), itemStyle: { color: PALETTE[5], borderRadius: [0, 6, 6, 0] },
    }],
  } : undefined

  const emptyOpt = {
    title: { text: '暂无数据', left: 'center', top: 'middle', textStyle: { color: 'rgba(128,128,145,.6)', fontSize: 13, fontWeight: 400 } },
  }

  const funnelOption: EChartsOption | undefined = st ? {
    tooltip: { trigger: 'axis' },
    grid: { left: 80, right: 24, top: 10, bottom: 26 },
    xAxis: { type: 'value', minInterval: 1, ...AXIS, splitLine: { lineStyle: { color: 'rgba(128,128,145,.15)' } } },
    yAxis: { type: 'category', data: ['消息', 'KB回答', '工具调用', '转人工', '拦截'], ...AXIS },
    series: [{
      name: '近 24 小时', type: 'bar', barWidth: 14,
      itemStyle: { color: PALETTE[4], borderRadius: [0, 6, 6, 0] },
      data: [st.funnel.messages, st.funnel.kb_answered, st.funnel.tool_calls, st.funnel.escalations, st.funnel.guardrail_blocked],
    }],
  } : undefined

  return (
    <div className="p-4 md:p-6 max-w-[1400px] mx-auto w-full space-y-4 pb-24 md:pb-10">
      <PageHead
        title="运行总览"
        sub="会话、渠道与工作流的实时状态 · 30 秒自动刷新"
        right={
          <>
            <Link to="/logs" className="btn ghost sm"><ScrollText size={13} /> 日志</Link>
            <Link to="/sessions" className="btn ghost sm"><MessagesSquare size={13} /> 会话</Link>
            {adminToken && (
              <span className="badge ok"><Lock size={11} /> 令牌已登录</span>
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
                    {m === 'auto' ? '自动' : m === 'llm' ? 'LLM' : '规则'}
                  </button>
                ))}
              </div>
            )}
          </>
        }
      />

      {needsToken && (
        <div className="bg-panel border border-line-2 rounded-xl p-4 flex flex-wrap items-center gap-3">
          <Lock size={16} className="text-warn" />
          <span className="text-sm">管理功能需要令牌（服务端设置了 ADMIN_TOKEN）</span>
          <input
            type="password"
            className="flex-1 min-w-[160px]"
            placeholder="输入 ADMIN_TOKEN"
            value={tokenInput}
            onChange={(e) => setTokenInput(e.target.value)}
          />
          <button
            className="btn sm"
            onClick={() => { setAdminToken(tokenInput.trim()); setTimeout(() => { overview.refetch(); stats.refetch() }, 50) }}
            disabled={!tokenInput.trim()}
          >
            登录
          </button>
          {adminToken && (
            <button className="btn ghost sm" onClick={() => { setAdminToken(''); setTokenInput('') }}>清除</button>
          )}
        </div>
      )}

      <div className="bg-panel border border-line-2 rounded-xl p-4 flex flex-wrap items-center gap-x-6 gap-y-2">
        <div className="flex items-center gap-2">
          <Bot size={16} className="text-brand" />
          <span className="font-semibold text-sm">Agent 引擎</span>
          <span className={`badge ${h?.mode === 'llm_react' ? 'ok' : 'info'}`}>
            {h ? (h.mode === 'llm_react' ? `LLM · ${h.model}` : '规则 + 检索') : '…'}
          </span>
        </div>
        <HealthChip ok={!!h?.llm_ready} on={h?.llm_ready ? 'LLM Key 已配置' : '未配 LLM Key（自动降级）'} />
        <HealthChip ok={true} on={`知识库 ${h?.kb_entries ?? '…'} 条`} />
        <HealthChip ok={true} on={`工具 ${h?.tool_count ?? '…'} 个`} />
        <HealthChip ok={true} on={`护栏规则 ${h?.injection_rules ?? '…'} 条`} />
        <Link to="/settings/params" className="text-xs text-brand hover:underline ml-auto">进入管理中心 →</Link>
      </div>

      {ov && ov.pending > 0 && (
        <Link to="/operator" className="block bg-warn-soft border border-warn rounded-xl p-4 flex items-center gap-3 hover:opacity-90 transition">
          <AlertTriangle size={18} className="text-warn" />
          <span className="text-sm font-medium">有 {ov.pending} 个会话等待人工接管</span>
          <span className="btn sm ml-auto"><Headset size={13} /> 前往坐席台</span>
        </Link>
      )}

      {overview.isLoading || !ov ? (
        <Spinner full label="加载总览…" />
      ) : overview.error ? null : (
        <div className="stat-grid">
          <StatCard icon={<Inbox size={18} />} value={ov.sessions} label="总会话" tone="brand" />
          <StatCard icon={<MessageSquare size={18} />} value={ov.messages} label="消息量" tone="ok" />
          <StatCard icon={<FileText size={18} />} value={ov.tickets} label="工单" tone="warn" />
          <StatCard icon={<Users size={18} />} value={ov.pending} label="待接管" tone="danger" />
          <StatCard icon={<BookOpen size={18} />} value={ov.kb_count} label="知识库" tone="brand" />
          <StatCard icon={<ShieldAlert size={18} />} value={ov.injection} label="注入拦截" tone="danger" />
          <StatCard icon={<CheckCircle2 size={18} />} value={`${(ov.feedback.help_rate * 100).toFixed(0)}%`} label="好评率" tone="ok" />
        </div>
      )}

      {stats.error && !needsToken && <ErrorBanner message={(stats.error as Error).message} onRetry={() => stats.refetch()} />}

      {st && (
        <div className="grid gap-4 md:grid-cols-2">
          <ChartCard title="近 24 小时消息量" icon={<MessageSquare size={15} />}>
            <EChart option={hourOption ?? emptyOpt} height={250} />
          </ChartCard>
          <ChartCard title="近 7 天新会话" icon={<Activity size={15} />}>
            <EChart option={dayOption ?? emptyOpt} height={250} />
          </ChartCard>
          <ChartCard title="渠道分布" icon={<Bot size={15} />}>
            <EChart option={channelOption ?? emptyOpt} height={250} />
          </ChartCard>
          <ChartCard title="升级转人工原因" icon={<Headset size={15} />}>
            <EChart option={escalateOption ?? emptyOpt} height={250} />
          </ChartCard>
          <ChartCard title="工具调用排行" icon={<Zap size={15} />}>
            <EChart option={toolOption ?? emptyOpt} height={250} />
          </ChartCard>
          <ChartCard title="反馈构成" icon={<CheckCircle2 size={15} />}>
            <EChart option={feedbackOption ?? emptyOpt} height={250} />
          </ChartCard>
          <ChartCard title="渠道连接" icon={<Radio size={15} />}>
            {chs && chs.length > 0 ? (
              <div className="h-[250px] flex flex-col justify-center gap-4 px-2">
                {chs.map((c) => (
                  <div key={c.channel} className="flex items-center gap-3 text-sm">
                    <span className={`w-2.5 h-2.5 rounded-full shrink-0 ${c.status === 'online' ? 'bg-ok' : c.status === 'stale' ? 'bg-warn' : c.status === 'builtin' ? 'bg-brand' : 'bg-danger'}`} />
                    <span className="font-medium w-[110px]">{c.channel}</span>
                    <span className="text-xs text-muted">{channelText(c)}</span>
                  </div>
                ))}
                <p className="text-[11px] text-muted">QQ 网关离线？启动 NapCat(:3001) 与 gateway/bot.py，每 30 秒自动心跳上报。</p>
              </div>
            ) : (
              <EChart option={emptyOpt} height={250} />
            )}
          </ChartCard>
          <ChartCard title="工作流漏斗（近 24 小时）" icon={<Activity size={15} />}>
            <EChart option={funnelOption ?? emptyOpt} height={250} />
          </ChartCard>
        </div>
      )}

      {st && st.messages_by_hour.every((m) => !m.visitor && !m.agent && !m.operator) && (
        <EmptyState
          icon={<MessageSquare size={22} />}
          title="还没有对话数据"
          desc={
            <span>
              打开<Link to="/" className="text-brand mx-1 hover:underline">访客对话页</Link>
              发一条消息，或通过 QQ 群 @机器人，图表就会实时亮起来。
            </span>
          }
        />
      )}
    </div>
  )
}

function HealthChip({ ok, on }: { ok: boolean; on: string }) {
  return (
    <span className={`badge ${ok ? 'muted' : 'warn'} text-[11px]`}>{on}</span>
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
