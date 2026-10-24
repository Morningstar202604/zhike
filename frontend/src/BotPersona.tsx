import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Bot, Save, RotateCcw, Zap, Send, Sparkles, Headset,
  Wrench, Heart, MessageCircle, Check, Loader2, FlaskConical,
} from 'lucide-react'
import { useConfig, useUpdateConfig, http } from './lib/queries'
import { useShell } from './components/ResponsiveShell'
import { Spinner } from './components/Spinner'
import TokenGate from './components/TokenGate'
import { isAuthError } from './lib/queries'
import type { AgentConfig } from './types/api'
import './pages2.css'

/* ============ 角色预设：一键应用人设与推荐温度 ============ */
const PRESETS: {
  key: string
  name: string
  icon: React.ReactNode
  desc: string
  persona: string
  temperature: number
}[] = [
  {
    key: 'standard',
    name: '标准客服',
    icon: <Headset size={16} />,
    desc: '专业可靠，全面解答，适合大多数店铺',
    persona:
      '你是知客，专业的在线客服 Agent，语气友好、简洁、可靠，使用中文回答。回答基于知识库与可用工具，不确定时如实告知并引导转人工。',
    temperature: 0.2,
  },
  {
    key: 'presale',
    name: '售前顾问',
    icon: <Sparkles size={16} />,
    desc: '主动推荐，善于引导下单与留资',
    persona:
      '你是知客，一名懂产品、会聊天的售前顾问。语气热情但不油腻，善于用一句话点出产品亮点，主动询问预算与使用场景，给出针对性推荐；报价前先确认需求，合适时机引导下单或留下联系方式。对库存、优惠等不确定的信息如实说明并转人工确认。',
    temperature: 0.5,
  },
  {
    key: 'tech',
    name: '技术支持',
    icon: <Wrench size={16} />,
    desc: '结构化排障，步步给出可执行操作',
    persona:
      '你是知客，一名耐心的技术支持工程师。回答先用一句话定位问题，再按「1/2/3」给出可执行的操作步骤，注明每步的预期结果；涉及版本、环境等信息主动追问；三步无法解决的明确告知将升级人工工单，并汇总已尝试的步骤。',
    temperature: 0.15,
  },
  {
    key: 'brand',
    name: '品牌小助手',
    icon: <Heart size={16} />,
    desc: '亲切自然，有温度的品牌人格',
    persona:
      '你是知客，品牌的小助手，像一个熟悉店铺的老朋友：语气亲切自然，会关心用户，回答简短口语化；遇到不满先共情再解决，绝不机械回复。所有承诺以知识库为准，不夸大、不随意承诺。',
    temperature: 0.7,
  },
  {
    key: 'minimal',
    name: '极简高效',
    icon: <MessageCircle size={16} />,
    desc: '惜字如金，一句话解决就不说第二句',
    persona:
      '你是知客，极简风格的客服。每条回复尽量不超过两句话：直接给结论或操作步骤，不寒暄、不重复用户问题、不输出与问题无关的内容。知识库没有依据的信息一律不编造。',
    temperature: 0.2,
  },
]

export default function BotPersona() {
  const { mobile } = useShell()
  const { data: cfgRes, isLoading, error, refetch } = useConfig()
  const upd = useUpdateConfig()
  const cfg = cfgRes?.config

  // 草稿状态：与服务器配置分离，保存后对齐
  const [draft, setDraft] = useState<AgentConfig | null>(null)
  useEffect(() => {
    if (cfg && !draft) setDraft(structuredClone(cfg))
  }, [cfg, draft])
  // 保存成功后（invalidate 拉回新配置）让草稿对齐服务器
  useEffect(() => {
    if (cfg && upd.isSuccess) setDraft(structuredClone(cfg))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [upd.isSuccess])

  const dirty = useMemo(() => {
    if (!cfg || !draft) return false
    return JSON.stringify(pick(draft)) !== JSON.stringify(pick(cfg))
  }, [cfg, draft])

  const patch = (fn: (d: AgentConfig) => void) => {
    setDraft((d) => {
      if (!d) return d
      const next = structuredClone(d)
      fn(next)
      return next
    })
  }

  const save = () => {
    if (!draft) return
    upd.mutate(pick(draft))
  }
  const reset = () => cfg && setDraft(structuredClone(cfg))

  const activePreset = useMemo(() => {
    if (!draft) return null
    return PRESETS.find((p) => p.persona === draft.context.persona) ?? null
  }, [draft])

  if (isLoading || (cfg && !draft)) return <Spinner full label="加载机器人配置…" />
  if (error && isAuthError(error)) return <TokenGate error={error} onAuthed={() => refetch()} />
  if (error) return <div className="p-8 text-sm text-[#c93034]">加载失败：{(error as Error).message}</div>
  if (!draft) return <TokenGate onAuthed={() => refetch()} />

  return (
    <div className={`bp-app ${mobile ? 'bp-mobile' : ''}`}>
      <div className="bp-head">
        <div>
          <h1 className="bp-title">机器人角色</h1>
          <p className="bp-sub">人设 · 提示词 · 生成参数 · 改完即时试问</p>
        </div>
        <div className="bp-head-actions">
          {dirty && <span className="bp-dirty">未保存</span>}
          <button className="btn secondary" onClick={reset} disabled={!dirty || upd.isPending}>
            <RotateCcw size={14} /> 重置
          </button>
          <button className="btn" onClick={save} disabled={!dirty || upd.isPending}>
            {upd.isPending ? <Loader2 size={14} className="animate-spin" /> : <Save size={14} />}
            {upd.isPending ? '保存中…' : upd.isSuccess && !dirty ? '已保存' : '保存配置'}
          </button>
        </div>
      </div>

      {/* 角色预设 */}
      <section className="bp-sec">
        <div className="bp-sec-head">
          <h2>角色预设</h2>
          <span>点击即应用到下方提示词，可再手动微调</span>
        </div>
        <div className="bp-presets">
          {PRESETS.map((p) => {
            const on = activePreset?.key === p.key
            return (
              <button
                key={p.key}
                className={`bp-preset ${on ? 'on' : ''}`}
                onClick={() =>
                  patch((d) => {
                    d.context.persona = p.persona
                    d.llm.temperature = p.temperature
                  })
                }
              >
                <span className="bp-preset-ico">{p.icon}</span>
                <span className="bp-preset-name">
                  {p.name}
                  {on && <Check size={13} className="bp-preset-check" />}
                </span>
                <span className="bp-preset-desc">{p.desc}</span>
              </button>
            )
          })}
        </div>
      </section>

      {/* 提示词编辑器 */}
      <section className="bp-sec">
        <div className="bp-sec-head">
          <h2>系统提示词（人设）</h2>
          <span>{draft.context.persona.length} 字 · 每轮对话都会注入</span>
        </div>
        <textarea
          className="bp-editor"
          rows={8}
          value={draft.context.persona}
          onChange={(e) => patch((d) => { d.context.persona = e.target.value })}
          placeholder="你是…（身份、语气、行为边界、知识库依据要求）"
        />
        <div className="bp-hint">
          建议包含：身份与语气 · 回答依据（知识库/工具） · 不确定时的处理（转人工） · 禁止事项。
          改动只影响后续新消息，进行中的会话按各自上下文继续。
        </div>
      </section>

      <div className="bp-cols">
        {/* 引擎与参数 */}
        <section className="bp-sec">
          <div className="bp-sec-head"><h2>引擎模式</h2></div>
          <div className="mod-row">
            {(['auto', 'llm', 'rule'] as const).map((m) => (
              <button
                key={m}
                className={`mod-toggle ${draft.engine.mode === m ? 'on' : ''}`}
                onClick={() => patch((d) => { d.engine.mode = m })}
              >
                <Zap size={12} />
                {m === 'auto' ? '自动' : m === 'llm' ? '强制 LLM' : '纯规则'}
              </button>
            ))}
          </div>
          <p className="bp-hint">
            {draft.engine.mode === 'auto' && '有可用 LLM Key 时走大模型，否则自动降级规则引擎。'}
            {draft.engine.mode === 'llm' && '强制走大模型；Key 失效时会标注 rule_fallback 并兜底。'}
            {draft.engine.mode === 'rule' && '完全用规则 ReAct 引擎，不调用大模型。'}
          </p>

          <div className="bp-sec-head" style={{ marginTop: 18 }}><h2>生成参数</h2></div>
          <Slider label="创造性 · temperature" value={draft.llm.temperature} min={0} max={2} step={0.05}
            onChange={(v) => patch((d) => { d.llm.temperature = v })} />
          <Slider label="核采样 · top_p" value={draft.llm.top_p} min={0} max={1} step={0.05}
            onChange={(v) => patch((d) => { d.llm.top_p = v })} />
          <Slider label="回复上限 · max_tokens" value={draft.llm.max_tokens} min={128} max={4096} step={64}
            onChange={(v) => patch((d) => { d.llm.max_tokens = v })} />
          <Slider label="ReAct 步数" value={draft.escalation.react_steps} min={1} max={8} step={1}
            onChange={(v) => patch((d) => { d.escalation.react_steps = v })} />
        </section>

        {/* 即时试问 */}
        <TryPanel />
      </div>
    </div>
  )
}

function Slider({ label, value, min, max, step, onChange }: {
  label: string; value: number; min: number; max: number; step: number
  onChange: (v: number) => void
}) {
  return (
    <div className="bp-slider">
      <div className="bp-slider-head"><span>{label}</span><b>{value}</b></div>
      <input type="range" min={min} max={max} step={step} value={value}
        onChange={(e) => onChange(parseFloat(e.target.value))} />
    </div>
  )
}

/* ============ 即时试问：开一个临时会话，用当前配置实测回复 ============ */
function TryPanel() {
  const [question, setQuestion] = useState('')
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<{ q: string; text: string; mode?: string; latency?: number; citations?: string[] } | null>(null)
  const [err, setErr] = useState('')
  const sidRef = useRef<string | null>(null)
  const boxRef = useRef<HTMLDivElement>(null)

  const ask = async () => {
    const q = question.trim()
    if (!q || busy) return
    setBusy(true); setErr('')
    const t0 = performance.now()
    try {
      let sid = sidRef.current
      if (!sid) {
        const data = await http.post<{ session_id: string; session_secret?: string }>('/api/sessions', {})
        sid = data.session_id
        sidRef.current = sid
        if (data.session_secret) {
          const { useAppStore } = await import('./store/appStore')
          useAppStore.getState().setSessionSecret(sid, data.session_secret)
        }
      }
      const r = await http.post<{ text: string; mode?: string; citations?: string[] }>(
        `/api/sessions/${sid}/messages`, { content: q })
      setResult({
        q,
        text: r.text,
        mode: r.mode,
        citations: r.citations || [],
        latency: Math.round(performance.now() - t0),
      })
      setQuestion('')
      requestAnimationFrame(() => boxRef.current?.scrollTo({ top: 1e6, behavior: 'smooth' }))
    } catch (e) {
      setErr((e as Error).message || '试问失败')
    } finally {
      setBusy(false)
    }
  }

  const resetSession = () => { sidRef.current = null; setResult(null); setErr('') }

  return (
    <section className="bp-sec bp-try">
      <div className="bp-sec-head">
        <h2><FlaskConical size={14} className="bp-flask" /> 即时试问</h2>
        <button className="btn ghost sm" onClick={resetSession} title="丢弃临时会话，下次试问将全新开始">新会话</button>
      </div>
      <p className="bp-hint" style={{ marginTop: 0 }}>
        用访客视角实测当前人设与参数：先点「保存配置」再试问，所见即访客所得。
      </p>
      <div className="bp-try-log" ref={boxRef}>
        {result && (
          <div className="bp-try-msg">
            <div className="bp-try-q"><span>{result.q}</span></div>
            <div className="bp-try-a">
              <Bot size={14} className="bp-try-bot" />
              <div>
                <div className="bp-try-text">{result.text}</div>
                <div className="bp-try-meta">
                  {result.mode && <em>{modeLabel(result.mode)}</em>}
                  {result.latency != null && <em>{(result.latency / 1000).toFixed(1)}s</em>}
                  {result.citations?.length ? <em>依据 {result.citations.join(' / ')}</em> : null}
                </div>
              </div>
            </div>
          </div>
        )}
        {!result && !busy && <div className="bp-try-empty">还没有试问记录。</div>}
        {busy && <div className="bp-try-empty"><Loader2 size={14} className="animate-spin inline-block" /> 正在生成…</div>}
        {err && <div className="bp-try-err">{err}</div>}
      </div>
      <div className="bp-try-input">
        <input
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ask() } }}
          placeholder="如：退货政策是什么 / 帮我推荐一款…"
          disabled={busy}
        />
        <button className="btn sm" onClick={ask} disabled={busy || !question.trim()}>
          <Send size={13} /> 试问
        </button>
      </div>
    </section>
  )
}

function modeLabel(mode: string): string {
  const map: Record<string, string> = {
    llm_react: 'LLM', llm: 'LLM', rule_react: '规则引擎', rule_fallback: '规则兜底',
    guardrail: '安全拦截', fallback: '异常兜底',
  }
  return map[mode] || mode
}

/** 只挑本页负责的配置组，避免整包覆盖其它面板未加载的字段 */
function pick(cfg: AgentConfig): Partial<AgentConfig> {
  return {
    context: {
      ...cfg.context,
      modules: { ...cfg.context.modules },
    },
    engine: { ...cfg.engine },
    llm: { ...cfg.llm, api_key: cfg.llm.api_key ?? '' },
    escalation: { ...cfg.escalation },
  }
}
