import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Bot, Save, RotateCcw, Zap, Send, Sparkles, Headset,
  Wrench, Heart, MessageCircle, Check, Loader2, FlaskConical,
  Trash2, Download, Upload, Briefcase, BarChart3, GraduationCap,
  CalendarCheck, UserCheck, Lightbulb, PenLine, CalendarClock, HeartHandshake,
} from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { useConfig, useUpdateConfig, http, isAuthError } from './lib/queries'
import { useShell } from './components/ResponsiveShell'
import { Spinner } from './components/Spinner'
import TokenGate from './components/TokenGate'
import { useFeedback } from './components/Feedback'
import type { AgentConfig } from './types/api'
import './pages2.css'
import { t } from './lib/t'

/* ============ 角色预设：文案由后端 core/personas.py 单一事实源下发 ============ */
interface PersonaPreset {
  id: string
  name: string
  category: string
  icon: string
  description: string
  temperature: number
  channels: string[]
  tags: string[]
  persona_chars: number
  persona: string
}
interface PersonaPayload { categories: string[]; presets: PersonaPreset[]; default_id: string }

const PRESET_ICONS: Record<string, React.ReactNode> = {
  Zap: <Zap size={16} />,
  Briefcase: <Briefcase size={16} />,
  MessageCircle: <MessageCircle size={16} />,
  Sparkles: <Sparkles size={16} />,
  Wrench: <Wrench size={16} />,
  BarChart3: <BarChart3 size={16} />,
  FlaskConical: <FlaskConical size={16} />,
  GraduationCap: <GraduationCap size={16} />,
  CalendarCheck: <CalendarCheck size={16} />,
  UserCheck: <UserCheck size={16} />,
  Lightbulb: <Lightbulb size={16} />,
  PenLine: <PenLine size={16} />,
  CalendarClock: <CalendarClock size={16} />,
  HeartHandshake: <HeartHandshake size={16} />,
  Heart: <Heart size={16} />,
  Headset: <Headset size={16} />,
}

/* ============ LLM 提供商管理（AstrBot 多提供商对标）：
   保存 base_url+model 档案（Key 只跟随当前激活配置），一键切换 ============ */
function ProviderPanel({ draft, patch }: { draft: AgentConfig; patch: (fn: (d: AgentConfig) => void) => void }) {
  const providers = draft.llm.providers || []
  const isCurrent = (p: { base_url: string; model: string }) =>
    p.base_url === draft.llm.base_url && p.model === draft.llm.model

  const saveCurrent = () => {
    const name = `${draft.llm.model} @ ${new URL(draft.llm.base_url).host}`
    if (providers.some((p) => p.name === name)) return
    patch((d) => {
      d.llm.providers = [...(d.llm.providers || []), { name, base_url: d.llm.base_url, model: d.llm.model }]
    })
  }
  const apply = (p: { name: string; base_url: string; model: string }) =>
    patch((d) => { d.llm.base_url = p.base_url; d.llm.model = p.model })
  const remove = (name: string) =>
    patch((d) => { d.llm.providers = (d.llm.providers || []).filter((p) => p.name !== name) })
  const setKey = (name: string, v: string) =>
    patch((d) => { const t = (d.llm.providers || []).find((x) => x.name === name); if (t) t.api_key = v })

  return (
    <section className="bp-sec">
      <div className="bp-sec-head">
        <h2>{t('LLM 提供商')}</h2>
        <span>{t('备援链随行待命：主模型挂了自动切下一个；同站点未填 Key 自动沿用主 Key，异站需填自己的 Key')}</span>
      </div>
      <div className="bp-roles">
        {providers.map((p) => (
          <div key={p.name} className={`bp-role ${isCurrent(p) ? 'bp-role-on' : ''}`}>
            <div className="bp-role-main" onClick={() => apply(p)} title={t('切换到此提供商')}>
              <b>{p.name}{isCurrent(p) && '（当前）'}</b>
              <span>{p.base_url}</span>
              <input
                type="password"
                placeholder={p.has_key ? `已存 Key（${p.api_key || '••••'}）· 点此可换` : '该站点的 API Key（同站可留空）'}
                value={p.api_key && !p.api_key.startsWith('••') ? p.api_key : ''}
                onClick={(e) => e.stopPropagation()}
                onChange={(e) => setKey(p.name, e.target.value)}
                style={{ marginTop: 4 }}
              />
            </div>
            {!isCurrent(p) && (
              <button className="btn secondary sm ac-del" title={t('删除档案')}
                onClick={() => remove(p.name)}>
                <Trash2 size={13} />
              </button>
            )}
          </div>
        ))}
      </div>
      <div className="bp-hint" style={{ marginTop: 8 }}>
        当前：{draft.llm.model} @ {draft.llm.base_url}（主 Key 在下方「LLM 接入」填写）
        {!providers.some((p) => isCurrent(p)) && (
          <button className="btn secondary sm" style={{ marginLeft: 10 }} onClick={saveCurrent}>
            把当前端点存为提供商
          </button>
        )}
      </div>
    </section>
  )
}

/* ============ 角色库（AstrBot 情景管理对标）：
   命名保存当前人设，随时切换/删除，支持导出导入 JSON 迁移 ============ */
const ROLE_LIB_KEY = 'zhike-persona-roles'
interface RoleEntry { name: string; persona: string; temperature: number; savedAt: string }

function loadRoles(): RoleEntry[] {
  try { return JSON.parse(localStorage.getItem(ROLE_LIB_KEY) || '[]') } catch { return [] }
}
function saveRoles(rs: RoleEntry[]) { localStorage.setItem(ROLE_LIB_KEY, JSON.stringify(rs)) }

function RoleLibrary({ draft, onApply }: { draft: AgentConfig; onApply: (r: RoleEntry) => void }) {
  const [roles, setRoles] = useState<RoleEntry[]>(loadRoles)
  const [name, setName] = useState('')
  const [expanded, setExpanded] = useState(false)

  const persist = (rs: RoleEntry[]) => { setRoles(rs); saveRoles(rs) }
  const saveCurrent = () => {
    const n = name.trim() || `角色 ${new Date().toLocaleDateString()}`
    const entry: RoleEntry = {
      name: n, persona: draft.context.persona,
      temperature: draft.llm.temperature, savedAt: new Date().toISOString(),
    }
    persist([entry, ...roles.filter((r) => r.name !== n)])
    setName('')
  }
  const exportJson = () => {
    const blob = new Blob([JSON.stringify(roles, null, 2)], { type: 'application/json' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = 'zhike-roles.json'
    a.click()
    URL.revokeObjectURL(a.href)
  }
  const importJson = (file: File) => {
    const rd = new FileReader()
    rd.onload = () => {
      try {
        const arr = JSON.parse(String(rd.result))
        if (!Array.isArray(arr)) throw new Error('格式不是角色数组')
        const merged = [...arr, ...roles.filter((r) => !arr.some((x: RoleEntry) => x.name === r.name))]
        persist(merged)
      } catch (e) { alert(`导入失败：${(e as Error).message}`) }
    }
    rd.readAsText(file)
  }

  return (
    <section className="bp-sec">
      <div className="bp-sec-head">
        <h2>{t('我的角色库')}</h2>
        <span>{roles.length} 个 · 保存在本浏览器，可导出迁移</span>
        <button className="btn ghost sm" style={{ marginLeft: 'auto' }} onClick={() => setExpanded((o) => !o)}>
          {expanded ? '收起' : '展开'}
        </button>
      </div>
      {expanded && (
        <>
          <div className="bp-role-save">
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={t('给当前人设起个名字，如：日程管家')}
              maxLength={40}
            />
            <button className="btn sm" onClick={saveCurrent} disabled={!draft.context.persona.trim()}>
              保存当前人设
            </button>
            <label className="btn secondary sm" style={{ cursor: 'pointer' }}>
              导入
              <input type="file" accept="application/json" style={{ display: 'none' }}
                onChange={(e) => { const f = e.target.files?.[0]; if (f) importJson(f); e.currentTarget.value = '' }} />
            </label>
            <button className="btn secondary sm" onClick={exportJson} disabled={!roles.length}>{t('导出')}</button>
          </div>
          {roles.length === 0 ? (
            <div className="bp-hint">{t('还没有保存的角色。调好提示词后起个名字存下来，随时一键切换。')}</div>
          ) : (
            <div className="bp-roles">
              {roles.map((r) => (
                <div key={r.name} className="bp-role">
                  <div className="bp-role-main" onClick={() => onApply(r)} title={t('应用此人设')}>
                    <b>{r.name}</b>
                    <span>{r.persona.slice(0, 46)}{r.persona.length > 46 ? '…' : ''}</span>
                    <em>temp {r.temperature} · {r.savedAt.slice(0, 10)}</em>
                  </div>
                  <button className="btn secondary sm ac-del" title={t('删除')}
                    onClick={() => persist(roles.filter((x) => x.name !== r.name))}>
                    <Trash2 size={13} />
                  </button>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </section>
  )
}

export default function BotPersona() {
  const { mobile } = useShell()
  const fb = useFeedback()
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
    upd.mutate(pick(draft), {
      onSuccess: () => fb.toast.success('机器人配置已保存并即时生效'),
      onError: (e) => fb.toast.error(`保存失败：${(e as Error).message}`),
    })
  }
  const reset = () => cfg && setDraft(structuredClone(cfg))

  const personasQ = useQuery({
    queryKey: ['personas'],
    queryFn: () => http.get<PersonaPayload>('/api/personas'),
    staleTime: 5 * 60_000,
  })
  const presets = personasQ.data?.presets ?? []
  const grouped = useMemo(() => {
    const order = personasQ.data?.categories ?? []
    const map = new Map<string, PersonaPreset[]>()
    for (const p of presets) {
      if (!map.has(p.category)) map.set(p.category, [])
      map.get(p.category)!.push(p)
    }
    return [...map.entries()].sort(
      (a, b) => order.indexOf(a[0]) - order.indexOf(b[0]),
    )
  }, [presets, personasQ.data?.categories])

  const activePreset = useMemo(() => {
    if (!draft) return null
    return presets.find((p) => p.persona === draft.context.persona) ?? null
  }, [draft, presets])

  if (isLoading || (cfg && !draft)) return <Spinner full label={t('加载机器人配置…')} />
  if (error && isAuthError(error)) return <TokenGate error={error} onAuthed={() => refetch()} />
  if (error) return <div className="p-8 text-sm text-[#c93034]">加载失败：{(error as Error).message}</div>
  if (!draft) return <TokenGate onAuthed={() => refetch()} />

  return (
    <div className={`bp-app ${mobile ? 'bp-mobile' : ''}`}>
      <div className="bp-head">
        <div>
          <h1 className="bp-title">{t('秘书人设')}</h1>
          <p className="bp-sub">{t('人设 · 提示词 · 生成参数 · 改完即时试问')}</p>
        </div>
        <div className="bp-head-actions">
          {dirty && <span className="bp-dirty">{t('未保存')}</span>}
          <button className="btn secondary" onClick={reset} disabled={!dirty || upd.isPending}>
            <RotateCcw size={14} /> 重置
          </button>
          <button className="btn" onClick={save} disabled={!dirty || upd.isPending}>
            {upd.isPending ? <Loader2 size={14} className="animate-spin" /> : <Save size={14} />}
            {upd.isPending ? '保存中…' : upd.isSuccess && !dirty ? '已保存' : '保存配置'}
          </button>
        </div>
      </div>

      {/* 角色预设：按分类分组渲染，文案来自后端 */}
      <section className="bp-sec">
        <div className="bp-sec-head">
          <h2>{t('角色预设')}</h2>
          <span>{t('点击即应用到下方提示词，可再手动微调')}</span>
        </div>
        {personasQ.isLoading && <div className="bp-hint">{t('加载预设角色…')}</div>}
        {personasQ.isError && (
          <div className="bp-hint">预设加载失败，可直接在下方编辑提示词。{(personasQ.error as Error).message}</div>
        )}
        {grouped.map(([cat, items]) => (
          <div key={cat} className="bp-preset-group">
            <div className="bp-preset-cat">{cat}</div>
            <div className="bp-presets">
              {items.map((p) => {
                const on = activePreset?.id === p.id
                return (
                  <button
                    key={p.id}
                    className={`bp-preset ${on ? 'on' : ''}`}
                    title={`${p.description}\n推荐温度 ${p.temperature} · ${p.persona_chars} 字`}
                    onClick={() =>
                      patch((d) => {
                        d.context.persona = p.persona
                        d.llm.temperature = p.temperature
                      })
                    }
                  >
                    <span className="bp-preset-ico">{PRESET_ICONS[p.icon] ?? <Bot size={16} />}</span>
                    <span className="bp-preset-name">
                      {p.name}
                      {on && <Check size={13} className="bp-preset-check" />}
                    </span>
                    <span className="bp-preset-desc">{p.description}</span>
                  </button>
                )
              })}
            </div>
          </div>
        ))}
      </section>

      {/* 角色库：命名保存 / 一键切换 / 导入导出 */}
      <RoleLibrary draft={draft} onApply={(r) =>
        patch((d) => {
          d.context.persona = r.persona
          d.llm.temperature = r.temperature
        })
      } />

      {/* 提示词编辑器 */}
      <section className="bp-sec">
        <div className="bp-sec-head">
          <h2>{t('系统提示词（人设）')}</h2>
          <span>{draft.context.persona.length} 字 · 每轮对话都会注入</span>
        </div>
        <textarea
          className="bp-editor"
          rows={8}
          value={draft.context.persona}
          onChange={(e) => patch((d) => { d.context.persona = e.target.value })}
          placeholder={t('你是…（身份、语气、行为边界、知识库依据要求）')}
        />
        <div className="bp-hint">
          建议包含：身份与语气 · 回答依据（知识库/工具） · 不确定时的处理 · 禁止事项。
          改动只影响后续新消息，进行中的会话按各自上下文继续。
        </div>
      </section>

      {/* LLM 提供商管理（AstrBot 多提供商对标） */}
      <ProviderPanel draft={draft} patch={patch} />

      {/* 渠道准入（chatgpt-on-wechat 群白名单对标） */}
      <section className="bp-sec">
        <div className="bp-sec-head">
          <h2>{t('渠道准入')}</h2>
          <span>{t('群白名单：开启后，白名单以外的群消息一律静默不答（私聊不受限）')}</span>
        </div>
        <div className="mod-row" style={{ marginBottom: 10 }}>
          <button
            className={`mod-toggle ${draft.channels?.allowlist_enabled ? 'on' : ''}`}
            onClick={() => patch((d) => {
              if (!d.channels) d.channels = { allowlist_enabled: false, group_allowlist: [] }
              d.channels.allowlist_enabled = !d.channels.allowlist_enabled
            })}
          >
            {draft.channels?.allowlist_enabled ? '白名单已开启' : '白名单已关闭'}
          </button>
        </div>
        <textarea
          className="bp-editor"
          rows={3}
          value={(draft.channels?.group_allowlist || []).join('\n')}
          onChange={(e) => patch((d) => {
            if (!d.channels) d.channels = { allowlist_enabled: false, group_allowlist: [] }
            d.channels.group_allowlist = e.target.value.split('\n').map((s) => s.trim()).filter(Boolean)
          })}
          placeholder={'每行一个群号，如：\n123456789\n987654321'}
        />
        <div className="bp-hint">
          群号可在 QQ 群设置里查看；机器人所在的群不在名单内时收到 @ 也不会回复，防止被拉进陌生群后被滥用。
        </div>
      </section>

      <div className="bp-cols">
        {/* 引擎与参数 */}
        <section className="bp-sec">
          <div className="bp-sec-head"><h2>{t('引擎模式')}</h2></div>
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

          <div className="bp-sec-head" style={{ marginTop: 18 }}><h2>{t('生成参数')}</h2></div>
          <Slider label={t('创造性 · temperature')} value={draft.llm.temperature} min={0} max={2} step={0.05}
            onChange={(v) => patch((d) => { d.llm.temperature = v })} />
          <Slider label={t('核采样 · top_p')} value={draft.llm.top_p} min={0} max={1} step={0.05}
            onChange={(v) => patch((d) => { d.llm.top_p = v })} />
          <Slider label={t('回复上限 · max_tokens')} value={draft.llm.max_tokens} min={128} max={4096} step={64}
            onChange={(v) => patch((d) => { d.llm.max_tokens = v })} />
          <Slider label={t('ReAct 步数')} value={draft.escalation.react_steps} min={1} max={8} step={1}
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
        <h2><FlaskConical size={14} className="bp-flask" /> {t('即时试问')}</h2>
        <button className="btn ghost sm" onClick={resetSession} title={t('丢弃临时会话，下次试问将全新开始')}>{t('新会话')}</button>
      </div>
      <p className="bp-hint" style={{ marginTop: 0 }}>
        用用户视角实测当前人设与参数：先点「保存配置」再试问，所见即用户所得。
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
        {!result && !busy && <div className="bp-try-empty">{t('还没有试问记录。')}</div>}
        {busy && <div className="bp-try-empty"><Loader2 size={14} className="animate-spin inline-block" /> {t('正在生成…')}</div>}
        {err && <div className="bp-try-err">{err}</div>}
      </div>
      <div className="bp-try-input">
        <input
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ask() } }}
          placeholder={t('如：退货政策是什么 / 帮我推荐一款…')}
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
    channels: cfg.channels
      ? { allowlist_enabled: cfg.channels.allowlist_enabled, group_allowlist: [...(cfg.channels.group_allowlist || [])] }
      : undefined,
  }
}
