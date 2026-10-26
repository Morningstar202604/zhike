import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'
import {
  Bot, Check, ChevronDown, ChevronUp, Eye, EyeOff, FlaskConical, Loader2,
  Lock, Plug, Plus, RefreshCw, Save, ShieldAlert, Sparkles, Trash2, Wand2,
} from 'lucide-react'
import { http } from './lib/queries'
import type { AgentConfig } from './types/api'
import { ErrorBanner } from './components/Spinner'
import { t } from './lib/t'

/* ============================================================
   模型厂商（预设 / 自动发现 / 能力匹配 / 连接测试）
   ============================================================ */

export interface Preset {
  name: string
  base_url: string
  protocol: string
  private_ok: boolean
  note: string
}

export interface DiscoveredModel {
  id: string
  display_name: string
  context_window: number
  max_output: number
  tool_calling: boolean | null
  source: string
}

interface ProviderDraft {
  name: string
  base_url: string
  protocol: string
  api_key: string
  model: string
  context_window: string
  max_output: string
  tool_calling: boolean
  models: DiscoveredModel[]
  private_ok: boolean
  saved: boolean
}

const EMPTY: ProviderDraft = {
  name: '', base_url: '', protocol: 'openai-compatible', api_key: '', model: '',
  context_window: '', max_output: '', tool_calling: true, models: [], private_ok: false,
  saved: false,
}

function fmtTokens(n?: number): string {
  if (!n || n <= 0) return '未知'
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1).replace(/\.0$/, '')}M`
  if (n >= 1_000) return `${Math.round(n / 1_000)}K`
  return String(n)
}

function canAutoDetect(f: { base_url: string; api_key: string; private_ok?: boolean }): boolean {
  if (!/^https?:\/\//i.test(f.base_url.trim())) return false
  return Boolean(f.api_key.trim()) || f.private_ok === true
}

/** 优先挑轻量档（flash/lite/mini/haiku），否则第一个 */
function pickDefault(models: DiscoveredModel[]): DiscoveredModel | undefined {
  return models.find((m) => /flash|lite|mini|haiku|small/i.test(m.id)) || models[0]
}

export function ProviderPanel({ cfg, setCfg, persist }: {
  cfg: AgentConfig
  setCfg: (next: AgentConfig) => void
  persist?: (next: AgentConfig) => void
}) {
  const qc = useQueryClient()
  const { data: meta } = useQuery({
    queryKey: ['provider-presets'],
    queryFn: () => http.get<{ presets: Preset[]; protocols: { value: string; label: string }[] }>('/api/providers/presets'),
  })
  const [open, setOpen] = useState(false)
  const [form, setForm] = useState<ProviderDraft>(EMPTY)
  const [discovering, setDiscovering] = useState(false)
  const [discoverMsg, setDiscoverMsg] = useState('')
  const [discoverErr, setDiscoverErr] = useState('')
  const [testing, setTesting] = useState(false)
  const [testMsg, setTestMsg] = useState('')
  const [testOk, setTestOk] = useState<boolean | null>(null)
  const [showKey, setShowKey] = useState(false)

  const providers = cfg.llm.providers || []
  const isActive = (p: { base_url: string; model: string }) =>
    p.base_url === cfg.llm.base_url && p.model === cfg.llm.model

  const patch = (p: Partial<ProviderDraft>) => setForm((f) => ({ ...f, ...p }))

  const discover = async (manual: boolean) => {
    if (!canAutoDetect(form)) {
      if (manual) setDiscoverErr('请先填写 Base URL；填 Key（或勾选内网端点）后即可检测')
      return
    }
    setDiscovering(true)
    setDiscoverErr('')
    setDiscoverMsg('')
    try {
      const r = await http.post<{ ok: boolean; models: DiscoveredModel[]; error?: string; note?: string }>(
        '/api/providers/discover',
        {
          base_url: form.base_url.trim(), api_key: form.api_key.trim(),
          protocol: form.protocol, private_ok: form.private_ok,
        },
      )
      if (!r.ok) {
        setDiscoverErr(r.error || '检测失败')
        return
      }
      setForm((cur) => {
        const next = { ...cur, models: r.models }
        if (r.models.length && !r.models.some((m) => m.id === cur.model)) {
          const pick = pickDefault(r.models)
          if (pick) {
            next.model = pick.id
            next.context_window = pick.context_window ? String(pick.context_window) : ''
            next.max_output = pick.max_output ? String(pick.max_output) : ''
            next.tool_calling = pick.tool_calling !== false
          }
        }
        return next
      })
      setDiscoverMsg(r.models.length
        ? `已检测到 ${r.models.length} 个可用模型，展开下方列表选择`
        : (r.note || '端点可达，但没有返回聊天模型，可手动填写模型名'))
    } catch (e) {
      setDiscoverErr((e as Error).message)
    } finally {
      setDiscovering(false)
    }
  }

  // 地址/Key 输入稳定 900ms 后自动检测：用户不用先点按钮才知道 Key 对不对
  useEffect(() => {
    if (!open || !canAutoDetect(form)) return
    const timer = setTimeout(() => { void discover(false) }, 900)
    return () => clearTimeout(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, form.base_url, form.api_key, form.protocol, form.private_ok])

  const applyModel = (m: DiscoveredModel) => {
    patch({
      model: m.id,
      context_window: m.context_window ? String(m.context_window) : form.context_window,
      max_output: m.max_output ? String(m.max_output) : form.max_output,
      tool_calling: m.tool_calling !== false,
    })
    setTestMsg('')
    setTestOk(null)
  }

  const runTest = async () => {
    if (!form.base_url.trim() || !form.model.trim()) {
      setTestOk(false)
      setTestMsg('请先填 Base URL 并选择模型')
      return
    }
    setTesting(true)
    setTestMsg('')
    setTestOk(null)
    try {
      const r = await http.post<{ ok: boolean; reply: string; latency_ms: number; model: string }>(
        '/api/providers/test',
        {
          base_url: form.base_url.trim(), api_key: form.api_key.trim(),
          protocol: form.protocol, model: form.model.trim(),
          max_output: Number(form.max_output) || 0, private_ok: form.private_ok,
        },
      )
      setTestOk(r.ok)
      setTestMsg(r.ok ? `${r.model} · ${r.latency_ms}ms · ${r.reply}` : r.reply)
    } catch (e) {
      setTestOk(false)
      setTestMsg((e as Error).message)
    } finally {
      setTesting(false)
    }
  }

  const save = () => {
    const name = form.name.trim() || `${form.model} @ ${form.base_url.replace(/^https?:\/\//, '').split('/')[0]}`
    const entry = {
      name,
      base_url: form.base_url.trim(),
      model: form.model.trim(),
      protocol: form.protocol,
      context_window: Number(form.context_window) || 0,
      max_output: Number(form.max_output) || 0,
      tool_calling: form.tool_calling,
      api_key: form.api_key.trim(),
    }
    const next = JSON.parse(JSON.stringify(cfg)) as AgentConfig
    next.llm.providers = [...(next.llm.providers || []).filter((p) => p.name !== name), entry]
    // 保存即激活：用户填完地址+Key+模型，期望的是「立刻能用」，不是再点一次激活
    next.llm.base_url = entry.base_url
    next.llm.model = entry.model
    next.llm.context_window = entry.context_window
    next.llm.max_output = entry.max_output
    next.llm.protocol = entry.protocol
    next.llm.tool_calling = entry.tool_calling
    if (entry.api_key) next.llm.api_key = entry.api_key
    setCfg(next)
    // 保存即落库：用户点「保存并启用」的语义就是"现在生效"，
    // 只改草稿还得再去另一个页签点保存，等于埋了一个「配置没生效」的坑。
    persist?.(next)
    setForm(EMPTY)
    setOpen(false)
  }

  const applyProvider = (p: {
    name: string; base_url: string; model: string
    context_window?: number; max_output?: number; protocol?: string
  }) => {
    const next = JSON.parse(JSON.stringify(cfg)) as AgentConfig
    next.llm.base_url = p.base_url
    next.llm.model = p.model
    if (p.context_window) next.llm.context_window = p.context_window
    if (p.max_output) next.llm.max_output = p.max_output
    if (p.protocol) next.llm.protocol = p.protocol
    setCfg(next)
    persist?.(next)
  }

  const remove = (name: string) => {
    const next = JSON.parse(JSON.stringify(cfg)) as AgentConfig
    next.llm.providers = (next.llm.providers || []).filter((p) => p.name !== name)
    setCfg(next)
    persist?.(next)
  }

  const setProviderKey = (name: string, v: string) => {
    const next = JSON.parse(JSON.stringify(cfg)) as AgentConfig
    const target = (next.llm.providers || []).find((p) => p.name === name)
    if (target) target.api_key = v
    setCfg(next)
  }

  const models = useMemo(() => form.models || [], [form.models])

  return (
    <section className="bp-sec">
      <div className="bp-sec-head">
        <h2><Plug size={15} /> {t('模型厂商')}</h2>
        <span>{t('填地址 + Key 自动检测可用模型，能力（上下文/输出/工具）自动匹配；三种主流协议')}</span>
      </div>

      <div className="flex flex-wrap items-center gap-2 mb-3">
        <span className="text-xs text-muted">{t('当前')}：</span>
        <code className="text-xs" style={{ background: 'var(--system-soft)', padding: '3px 8px', borderRadius: 6 }}>
          {cfg.llm.model} @ {cfg.llm.base_url}
        </code>
        {cfg.llm.has_key
          ? <span className="badge ok">{t('Key 已配置')}</span>
          : <span className="badge danger">{t('Key 未配置')}</span>}
      </div>

      {providers.length > 0 && (
        <div className="space-y-2 mb-3">
          {providers.map((p) => (
            <div key={p.name} className="rounded-lg border px-3 py-2" style={{ borderColor: 'var(--line)' }}>
              <div className="flex flex-wrap items-center gap-2">
                <span className={`w-2 h-2 rounded-full ${isActive(p) ? 'bg-emerald-500' : 'bg-slate-300'}`} />
                <b className="text-sm">{p.name}</b>
                <code className="text-xs text-muted flex-1 min-w-0 truncate">{p.model} · {p.base_url}</code>
                <span className="badge muted">{p.protocol || 'openai-compatible'}</span>
                {isActive(p)
                  ? <span className="badge ok">{t('使用中')}</span>
                  : <button className="btn sm" onClick={() => applyProvider(p)}>{t('切换到这个')}</button>}
                <button className="btn secondary sm" onClick={() => remove(p.name)} title={t('删除')}>
                  <Trash2 size={12} />
                </button>
              </div>
              <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted">
                <span>{t('上下文')} {fmtTokens(p.context_window)}</span>
                <span>·</span>
                <span>{t('输出上限')} {fmtTokens(p.max_output)}</span>
                <input
                  type="password"
                  placeholder={p.has_key ? t('已存 Key，留空不修改') : t('该站点 API Key')}
                  onChange={(e) => setProviderKey(p.name, e.target.value)}
                  style={{ maxWidth: 220, marginLeft: 'auto' }}
                />
              </div>
            </div>
          ))}
        </div>
      )}

      <button className="btn secondary sm" onClick={() => { setOpen((o) => !o); setForm(EMPTY); setTestMsg(''); setDiscoverMsg(''); setDiscoverErr('') }}>
        {open ? <ChevronUp size={13} /> : <Plus size={13} />} {open ? t('收起') : t('添加模型厂商')}
      </button>

      {open && (
        <div className="mt-3 rounded-lg border p-3 space-y-3" style={{ borderColor: 'var(--line)' }}>
          <div>
            <div className="text-xs text-muted mb-1">{t('常见厂商（只填地址与协议，模型由端点探测）')}</div>
            <div className="flex flex-wrap gap-1.5">
              {(meta?.presets || []).map((p) => (
                <button
                  key={p.name}
                  className="text-xs rounded-full border px-2.5 py-1"
                  style={{ borderColor: 'var(--line)', background: 'var(--panel)' }}
                  onClick={() => {
                    patch({ name: p.name, base_url: p.base_url, protocol: p.protocol, private_ok: p.private_ok, model: '', models: [] })
                    setDiscoverMsg('')
                    setDiscoverErr('')
                  }}
                  title={p.note || p.base_url}
                >
                  {p.name}
                </button>
              ))}
            </div>
          </div>

          <div className="grid sm:grid-cols-2 gap-3">
            <div>
              <div className="text-xs text-muted mb-1">{t('厂商名称')}</div>
              <input value={form.name} onChange={(e) => patch({ name: e.target.value })} placeholder={t('如 UniSound MaaS')} />
            </div>
            <div>
              <div className="text-xs text-muted mb-1">{t('协议')}</div>
              <select value={form.protocol} onChange={(e) => patch({ protocol: e.target.value, model: '', models: [] })}>
                {(meta?.protocols || [{ value: 'openai-compatible', label: 'OpenAI 兼容' }]).map((p) => (
                  <option key={p.value} value={p.value}>{p.label}</option>
                ))}
              </select>
            </div>
            <div className="sm:col-span-2">
              <div className="text-xs text-muted mb-1">Base URL</div>
              <input value={form.base_url} onChange={(e) => patch({ base_url: e.target.value })} placeholder="https://api.example.com/v1" />
            </div>
            <div className="sm:col-span-2">
              <div className="text-xs text-muted mb-1">API Key</div>
              <div className="flex items-center gap-2">
                <input
                  type={showKey ? 'text' : 'password'}
                  value={form.api_key}
                  onChange={(e) => patch({ api_key: e.target.value })}
                  placeholder="sk-..."
                  style={{ flex: 1 }}
                />
                <button className="btn ghost sm" onClick={() => setShowKey((s) => !s)} title={t('显示/隐藏')}>
                  {showKey ? <EyeOff size={13} /> : <Eye size={13} />}
                </button>
              </div>
            </div>
          </div>

          <label className="flex items-center gap-2 text-xs text-muted">
            <input type="checkbox" checked={form.private_ok} onChange={(e) => patch({ private_ok: e.target.checked })} />
            {t('本地 / 内网端点（Ollama、自建网关；云元数据地址仍拒绝）')}
          </label>

          <div className="rounded-lg border p-3" style={{ borderColor: 'var(--line)' }}>
            <div className="flex flex-wrap items-center justify-between gap-2 mb-2">
              <span className="flex items-center gap-2 text-sm font-medium">
                <Sparkles size={14} /> {t('可用模型（填完地址与 Key 自动检测）')}
              </span>
              <button className="btn secondary sm" onClick={() => void discover(true)} disabled={discovering || !canAutoDetect(form)}>
                {discovering ? <Loader2 size={13} className="animate-spin" /> : <RefreshCw size={13} />} {t('重新检测')}
              </button>
            </div>
            {discovering && <div className="text-xs text-muted flex items-center gap-2"><Loader2 size={13} className="animate-spin" /> {t('正在请求模型列表…')}</div>}
            {!discovering && discoverErr && <div className="text-xs" style={{ color: 'var(--danger)' }}>{discoverErr}</div>}
            {!discovering && !discoverErr && discoverMsg && <div className="text-xs text-muted">{discoverMsg}</div>}
            {models.length > 0 && (
              <div className="mt-2 max-h-60 overflow-y-auto rounded-md border" style={{ borderColor: 'var(--line)' }}>
                {models.map((m) => {
                  const chosen = form.model === m.id
                  return (
                    <button
                      key={m.id}
                      className="w-full text-left px-3 py-2 text-xs flex flex-wrap items-center gap-2"
                      style={{ background: chosen ? 'var(--brand-soft)' : 'transparent' }}
                      onClick={() => applyModel(m)}
                    >
                      {chosen ? <Check size={13} /> : <span style={{ width: 13 }} />}
                      <code>{m.id}</code>
                      <span className="ml-auto flex items-center gap-1.5 text-muted">
                        {m.context_window ? <span className="badge muted">{t('上下文')} {fmtTokens(m.context_window)}</span> : null}
                        {m.max_output ? <span className="badge muted">{t('输出')} {fmtTokens(m.max_output)}</span> : null}
                        <span className={`badge ${m.tool_calling === false ? 'warn' : 'ok'}`}>
                          {m.tool_calling === false ? t('无工具') : t('工具调用')}
                        </span>
                      </span>
                    </button>
                  )
                })}
              </div>
            )}
            <div className="mt-2">
              <div className="text-xs text-muted mb-1">{t('当前模型（可直接手填）')}</div>
              <input value={form.model} onChange={(e) => { patch({ model: e.target.value }); setTestOk(null); setTestMsg('') }} placeholder={t('从上方选择，或手填模型名')} />
            </div>
            <div className="mt-3 grid sm:grid-cols-3 gap-2">
              <div>
                <div className="text-xs text-muted mb-1">{t('上下文窗口')}</div>
                <input value={form.context_window} inputMode="numeric" onChange={(e) => patch({ context_window: e.target.value.replace(/\D/g, '') })} placeholder="0 = 未知" />
              </div>
              <div>
                <div className="text-xs text-muted mb-1">{t('单次输出上限')}</div>
                <input value={form.max_output} inputMode="numeric" onChange={(e) => patch({ max_output: e.target.value.replace(/\D/g, '') })} placeholder="0 = 未知" />
              </div>
              <label className="flex items-end gap-2 pb-2 text-xs text-muted">
                <input type="checkbox" checked={form.tool_calling} onChange={(e) => patch({ tool_calling: e.target.checked })} />
                {t('支持工具调用')}
              </label>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <button className="btn sm" onClick={() => void runTest()} disabled={testing || !form.base_url.trim() || !form.model.trim()}>
              {testing ? <Loader2 size={13} className="animate-spin" /> : <FlaskConical size={13} />} {t('测试连接')}
            </button>
            {testMsg && (
              <span className="text-xs" style={{ color: testOk ? 'var(--ok)' : 'var(--danger)' }}>
                {testOk ? <Check size={12} style={{ display: 'inline' }} /> : null} {testMsg}
              </span>
            )}
            <button className="btn secondary sm ml-auto" onClick={save} disabled={!form.base_url.trim() || !form.model.trim()}>
              <Save size={13} /> {t('保存并启用')}
            </button>
          </div>
        </div>
      )}
    </section>
  )
}

/* ============================================================
   技能（Skills）
   ============================================================ */

interface SkillItem {
  id: string; name: string; description: string; guide: string
  enabled: boolean; builtin: boolean
}

export function SkillsPanel() {
  const qc = useQueryClient()
  const { data, isLoading, error } = useQuery({
    queryKey: ['skills'],
    queryFn: () => http.get<{ items: SkillItem[] }>('/api/skills'),
  })
  const [items, setItems] = useState<SkillItem[]>([])
  const [newName, setNewName] = useState('')
  const [newDesc, setNewDesc] = useState('')
  const [newGuide, setNewGuide] = useState('')

  useEffect(() => { if (data?.items) setItems(data.items) }, [data])

  const save = useMutation({
    mutationFn: (next: SkillItem[]) => http.put<{ items: SkillItem[] }>('/api/skills', { items: next }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['skills'] }); qc.invalidateQueries({ queryKey: ['config'] }) },
  })

  if (isLoading) return <div className="p-4 text-sm text-muted"><Loader2 size={14} className="animate-spin inline" /> {t('加载技能…')}</div>
  if (error) return <ErrorBanner message={(error as Error).message} onRetry={() => qc.invalidateQueries({ queryKey: ['skills'] })} />

  const toggle = (id: string) =>
    setItems((prev) => prev.map((s) => (s.id === id ? { ...s, enabled: !s.enabled } : s)))

  const add = () => {
    if (!newName.trim()) return
    setItems((prev) => [...prev, {
      id: `custom-${Date.now()}`, name: newName.trim(), description: newDesc.trim(),
      guide: newGuide.trim() || newDesc.trim(), enabled: true, builtin: false,
    }])
    setNewName(''); setNewDesc(''); setNewGuide('')
  }

  return (
    <section className="bp-sec">
      <div className="bp-sec-head">
        <h2><Wand2 size={15} /> {t('技能')}</h2>
        <span>{t('技能是「什么时候用哪个工具」的说明书，每轮注入系统提示词；不是工具本身')}</span>
      </div>
      <div className="space-y-2">
        {items.map((s) => (
          <div key={s.id} className="rounded-lg border px-3 py-2" style={{ borderColor: 'var(--line)' }}>
            <div className="flex flex-wrap items-center gap-2">
              <label className="flex items-center gap-2 text-sm">
                <input type="checkbox" checked={s.enabled} onChange={() => toggle(s.id)} />
                <b>{s.name}</b>
              </label>
              {s.builtin ? <span className="badge muted">{t('内置')}</span> : <span className="badge info">{t('自定义')}</span>}
              <span className="text-xs text-muted flex-1">{s.description}</span>
              {!s.builtin && (
                <button className="btn ghost sm" onClick={() => setItems((prev) => prev.filter((x) => x.id !== s.id))}>
                  <Trash2 size={12} />
                </button>
              )}
            </div>
            <textarea
              rows={2}
              value={s.guide}
              onChange={(e) => setItems((prev) => prev.map((x) => (x.id === s.id ? { ...x, guide: e.target.value } : x)))}
              className="mt-1 w-full text-xs"
              placeholder={t('注入给模型的执行指南')}
            />
          </div>
        ))}
      </div>
      <div className="mt-3 rounded-lg border p-3 space-y-2" style={{ borderColor: 'var(--line)' }}>
        <div className="text-xs text-muted">{t('新增自定义技能')}</div>
        <input value={newName} onChange={(e) => setNewName(e.target.value)} placeholder={t('技能名称，如：竞品调研')} />
        <input value={newDesc} onChange={(e) => setNewDesc(e.target.value)} placeholder={t('一句话描述什么时候用它')} />
        <textarea rows={2} value={newGuide} onChange={(e) => setNewGuide(e.target.value)} placeholder={t('执行指南（不填则用描述）')} className="w-full text-xs" />
        <div className="flex gap-2">
          <button className="btn secondary sm" onClick={add} disabled={!newName.trim()}><Plus size={13} /> {t('添加')}</button>
          <button className="btn sm ml-auto" onClick={() => save.mutate(items)} disabled={save.isPending}>
            {save.isPending ? <Loader2 size={13} className="animate-spin" /> : <Save size={13} />} {t('保存技能')}
          </button>
        </div>
        {save.isError && <ErrorBanner message={(save.error as Error).message} onRetry={() => save.mutate(items)} />}
        {save.isSuccess && <div className="text-xs" style={{ color: 'var(--ok)' }}>{t('已保存，下一轮对话生效')}</div>}
      </div>
    </section>
  )
}

/* ============================================================
   钩子（Hooks）
   ============================================================ */

interface HookItem {
  id: string; name: string; event: string; match: string; action: string
  value: string; enabled: boolean; times: number
}

const EVENT_LABEL: Record<string, string> = {
  pre_input: '用户消息进管线前',
  post_reply: '回复落库前',
}
const ACTION_LABEL: Record<string, string> = {
  block: '拦截并回复固定话术',
  rewrite: '改写消息',
  append: '追加到回复末尾',
  replace: '整段替换回复',
}

export function HooksPanel() {
  const qc = useQueryClient()
  const { data, isLoading } = useQuery({
    queryKey: ['hooks'],
    queryFn: () => http.get<{ items: HookItem[]; actions: Record<string, string[]> }>('/api/hooks'),
  })
  const [items, setItems] = useState<HookItem[]>([])
  useEffect(() => { if (data?.items) setItems(data.items) }, [data])

  const save = useMutation({
    mutationFn: (next: HookItem[]) => http.put<{ items: HookItem[] }>('/api/hooks', { items: next }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['hooks'] }); qc.invalidateQueries({ queryKey: ['config'] }) },
  })

  if (isLoading) return <div className="p-4 text-sm text-muted"><Loader2 size={14} className="animate-spin inline" /> {t('加载钩子…')}</div>

  const add = () =>
    setItems((prev) => [...prev, {
      id: `hook-${Date.now()}`, name: '', event: 'pre_input', match: '',
      action: 'block', value: '', enabled: true, times: 0,
    }])

  const upd = (id: string, p: Partial<HookItem>) =>
    setItems((prev) => prev.map((h) => (h.id === id ? { ...h, ...p } : h)))

  return (
    <section className="bp-sec">
      <div className="bp-sec-head">
        <h2><ShieldAlert size={15} /> {t('钩子')}</h2>
        <span>{t('在管线固定位置强制执行的确定性规则：不依赖模型配合，每轮必然生效')}</span>
      </div>
      <div className="space-y-2">
        {items.map((h) => (
          <div key={h.id} className="rounded-lg border px-3 py-2 space-y-2" style={{ borderColor: 'var(--line)' }}>
            <div className="flex flex-wrap items-center gap-2">
              <input type="checkbox" checked={h.enabled} onChange={(e) => upd(h.id, { enabled: e.target.checked })} />
              <input value={h.name} onChange={(e) => upd(h.id, { name: e.target.value })}
                     placeholder={t('规则名称')} style={{ maxWidth: 180 }} />
              <select value={h.event} onChange={(e) => {
                const event = e.target.value
                const allowed = data?.actions?.[event] || ['block']
                upd(h.id, { event, action: allowed.includes(h.action) ? h.action : allowed[0] })
              }}>
                <option value="pre_input">{t(EVENT_LABEL.pre_input)}</option>
                <option value="post_reply">{t(EVENT_LABEL.post_reply)}</option>
              </select>
              <select value={h.action} onChange={(e) => upd(h.id, { action: e.target.value })}>
                {(data?.actions?.[h.event] || ['block']).map((a) => (
                  <option key={a} value={a}>{t(ACTION_LABEL[a] || a)}</option>
                ))}
              </select>
              <span className="text-xs text-muted">{t('命中')} {h.times}</span>
              <button className="btn ghost sm ml-auto" onClick={() => setItems((prev) => prev.filter((x) => x.id !== h.id))}>
                <Trash2 size={12} />
              </button>
            </div>
            <input value={h.match} onChange={(e) => upd(h.id, { match: e.target.value })}
                   placeholder={t('匹配正则（留空=对该事件所有消息生效）')} />
            <textarea rows={2} value={h.value} onChange={(e) => upd(h.id, { value: e.target.value })}
                      placeholder={t('动作内容：拦截话术 / 改写替换文本 / 追加后缀')} className="w-full text-xs" />
          </div>
        ))}
      </div>
      <div className="mt-3 flex gap-2">
        <button className="btn secondary sm" onClick={add}><Plus size={13} /> {t('新增钩子')}</button>
        <button className="btn sm ml-auto" onClick={() => save.mutate(items)} disabled={save.isPending}>
          {save.isPending ? <Loader2 size={13} className="animate-spin" /> : <Save size={13} />} {t('保存钩子')}
        </button>
      </div>
      {save.isSuccess && <div className="text-xs mt-2" style={{ color: 'var(--ok)' }}>{t('已保存，下一轮对话生效')}</div>}
    </section>
  )
}

/* ============================================================
   权限
   ============================================================ */

interface PermDesc {
  open_mode: boolean
  audit_changes: boolean
  locked: { id: string; label: string; how: string }[]
  open: { id: string; label: string }[]
}

export function PermissionsPanel() {
  const qc = useQueryClient()
  const { data, isLoading } = useQuery({
    queryKey: ['permissions'],
    queryFn: () => http.get<PermDesc>('/api/permissions'),
  })
  const save = useMutation({
    mutationFn: (patch: Partial<PermDesc>) => http.put<PermDesc>('/api/permissions', patch),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['permissions'] }); qc.invalidateQueries({ queryKey: ['config'] }) },
  })

  if (isLoading) return <div className="p-4 text-sm text-muted"><Loader2 size={14} className="animate-spin inline" /> {t('加载权限…')}</div>
  if (!data) return null

  return (
    <section className="bp-sec">
      <div className="bp-sec-head">
        <h2><ShieldAlert size={15} /> {t('权限')}</h2>
        <span>{t('全面放开：除核心影响项外全部可改；核心项只能改环境变量或 policy.json')}</span>
      </div>

      <label className="flex items-center gap-2 mb-3">
        <input type="checkbox" checked={data.open_mode}
               onChange={(e) => save.mutate({ open_mode: e.target.checked })} />
        <b className="text-sm">{t('全面放开')}</b>
        <span className="text-xs text-muted">
          {data.open_mode
            ? t('已开启：模型/检索/上下文/人设/技能/钩子/渠道/工具等全部可在界面修改')
            : t('已关闭：回到最小权限（Agent 自主度压到 T1，仅只读/辅助）')}
        </span>
      </label>

      <label className="flex items-center gap-2 mb-3">
        <input type="checkbox" checked={data.audit_changes}
               onChange={(e) => save.mutate({ audit_changes: e.target.checked })} />
        <span className="text-sm">{t('权限相关变更写审计链')}</span>
      </label>

      <div className="grid sm:grid-cols-2 gap-3">
        <div className="rounded-lg border p-3" style={{ borderColor: 'var(--line)' }}>
          <div className="text-xs font-semibold mb-2 flex items-center gap-1"><Lock size={12} /> {t('核心锁定（界面只读）')}</div>
          <ul className="space-y-2">
            {data.locked.map((x) => (
              <li key={x.id}>
                <div className="text-sm">{x.label}</div>
                <div className="text-xs text-muted">{x.how}</div>
              </li>
            ))}
          </ul>
        </div>
        <div className="rounded-lg border p-3" style={{ borderColor: 'var(--line)' }}>
          <div className="text-xs font-semibold mb-2 flex items-center gap-1"><Bot size={12} /> {t('可在界面修改')}</div>
          <div className="flex flex-wrap gap-1.5">
            {data.open.map((x) => (
              <span key={x.id} className="badge muted text-xs">{x.label}</span>
            ))}
          </div>
        </div>
      </div>
      {save.isError && <ErrorBanner message={(save.error as Error).message} onRetry={() => save.mutate({ open_mode: data.open_mode })} />}
    </section>
  )
}
