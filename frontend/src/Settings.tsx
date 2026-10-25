import { useEffect, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  SlidersHorizontal, BookOpen, BarChart3, Brain, ShieldAlert,
  Save, Plus, Pencil, Trash2, X, Activity, Inbox, MessageSquare,
  FileText, Users, Search, Wrench, Zap, ChevronDown, ChevronUp,
  ChevronLeft, ChevronRight, Upload, Download, KeyRound, Check } from 'lucide-react'
import {
  useConfig, useUpdateConfig, useKb, useKbMutations, useKbImport, useKbTest, useKbDocImport, exportKb, useTools, useToolMutations,
  useInjectionRules, useInjectionRuleMutations, useLlmTest, useLlmModels,
  useAdminOverview, useAdminMemory, useAdminInjection, useAdminReport, useAdminStats,
} from './lib/queries'
import { StatCard, EmptyState } from './ui'
import { useShell } from './components/ResponsiveShell'
import { Spinner, ErrorBanner } from './components/Spinner'
import type { AgentConfig, KbEntry, ToolDef, InjectionRule, MemoryEntry, InjectionLogEntry } from './types/api'
import { usePagination } from './lib/pagination'
import { isAuthError } from './lib/queries'
import { TOOL_LABEL } from './lib/labels'
import TokenGate from './components/TokenGate'
import { useAppStore } from './store/appStore'
import { useFeedback } from './components/Feedback'
import CardsPanel from './CardsPanel'
import './pages2.css'
import { t as tr } from './lib/t'

const TABS = [
  { key: 'params', icon: <SlidersHorizontal size={15} />, label: '能力参数', sub: '模型 / 检索 / 上下文 / 记忆 / 引擎' },
  { key: 'cards', icon: <Brain size={15} />, label: '角色卡', sub: 'SillyTavern 卡导入 / 按群绑定人格' },
  { key: 'tools', icon: <Wrench size={15} />, label: '工具注册表', sub: '可自定义 Agent 能执行的动作' },
  { key: 'guardrail', icon: <ShieldAlert size={15} />, label: '注入规则', sub: '护栏检测规则管理' },
  { key: 'kb', icon: <BookOpen size={15} />, label: '知识库', sub: 'RAG 语料增删改' },
  { key: 'data', icon: <BarChart3 size={15} />, label: '数据概览', sub: '运营统计' },
  { key: 'mem', icon: <Brain size={15} />, label: '长期记忆', sub: '跨会话沉淀' },
  { key: 'inj', icon: <Activity size={15} />, label: '注入日志', sub: '护栏审计' },
  { key: 'token', icon: <KeyRound size={15} />, label: '访问令牌', sub: '控制台登录凭据' },
]

export default function Settings() {
  const { tab: routeTab } = useParams()
  const navigate = useNavigate()
  const routeOk = TABS.some((t) => t.key === routeTab)
  const tab = routeOk ? (routeTab as string) : 'params'
  const setTab = (k: string) => navigate(`/settings/${k}`)
  const { mobile } = useShell()
  const { data: cfgRes, isLoading, error, refetch } = useConfig()
  const cfg = cfgRes?.config
  const upd = useUpdateConfig()
  // 配置草稿：所有控件只改本地状态，点「保存配置」才落库。
  // 之前每个 onChange 都 PUT 整包，滑杆拖动=请求风暴，响应乱序还会覆盖新值。
  const [draft, setDraft] = useState<AgentConfig | null>(null)
  useEffect(() => {
    setDraft(cfgRes?.config ?? null)
  }, [cfgRes])
  const shown = draft ?? cfg

  if (isLoading) return <Spinner full label={tr('加载配置…')} />
  // 401/403/503 都以令牌门禁承接：以前这里渲染 ErrorBanner，用户无法就地登录
  if (error && isAuthError(error)) return <TokenGate error={error} onAuthed={() => refetch()} />
  if (error) return <div className="p-8"><ErrorBanner message={(error as Error).message} onRetry={() => refetch()} /></div>
  if (!cfg) return <TokenGate onAuthed={() => refetch()} />

  const active = TABS.find((t) => t.key === tab)!

  return (
    <div className={`set-app ${mobile ? 'set-app-mobile' : ''}`}>
      <aside className="set-side">
        <div className="ss-title">{tr('管理中心')}</div>
        <div className="ss-sub">{tr('Agent 运行时配置 · 即时生效')}</div>
        {TABS.map((item) => (
          <button key={item.key} className={`set-item ${tab === item.key ? 'on' : ''}`} onClick={() => setTab(item.key)}>
            <span className="si-ico">{item.icon}</span>
            <span>{tr(item.label)}</span>
          </button>
        ))}
      </aside>

      <main className="set-main">
        <div className="set-main-head">
          <div>
            <h1>{tr(active.label)}</h1>
            <p>{tr(active.sub)}</p>
          </div>
          {tab === 'params' && (
            <button className="btn" onClick={() => shown && upd.mutate(shown)}
                    disabled={upd.isPending || !shown}>
              <Save size={14} />
              {upd.isPending ? '保存中…' : upd.isSuccess ? '已保存' : '保存配置'}
            </button>
          )}
        </div>

        {tab === 'params' && upd.isError && (
          <div className="mb-3">
            <ErrorBanner message={(upd.error as Error).message} onRetry={() => upd.mutate(shown!)} />
          </div>
        )}
        {tab === 'params' && shown && <ParamsPanel cfg={shown} setDraft={setDraft} upd={upd} />}
        {tab === 'cards' && <CardsPanel />}
        {tab === 'tools' && <ToolsPanel />}
        {tab === 'guardrail' && <GuardrailPanel />}
        {tab === 'kb' && <KbPanel />}
        {tab === 'data' && <DataPanel />}
        {tab === 'mem' && <MemPanel />}
        {tab === 'inj' && <InjPanel />}
        {tab === 'token' && <TokenPanel />}
      </main>
    </div>
  )
}

/* ============ 参数配置（补齐 char_budget/react_steps/top_p + 引擎模式） ============ */
function ParamsPanel({ cfg, setDraft, upd }: {
  cfg: AgentConfig
  setDraft: React.Dispatch<React.SetStateAction<AgentConfig | null>>
  upd: ReturnType<typeof useUpdateConfig>
}) {
  const { data: kbMeta } = useKb()
  const llmTest = useLlmTest()
  const llmModels = useLlmModels()
  const [apiKeyInput, setApiKeyInput] = useState('')
  const modelsRes = llmModels.data
  const modelsLoaded = !!(modelsRes && modelsRes.ok && modelsRes.models.length > 0)
  const set = (group: keyof AgentConfig, key: string, value: unknown) => {
    setDraft((prev) => {
      const base = structuredClone(prev ?? cfg) as AgentConfig
      ;(base[group] as Record<string, unknown>)[key] = value
      return base
    })
  }

  const Grp = ({ id, title, note, children }: { id: string; title: string; note?: string; children: React.ReactNode }) => {
    const [open, setOpen] = useState(true)
    return (
      <div className="grp">
        <div className="grp-head" onClick={() => setOpen((o) => !o)}>
          <h3>{title}</h3>
          {note && <span className="gh-note">{note}</span>}
          <span className="text-muted text-xs">{open ? <ChevronUp size={15} /> : <ChevronDown size={15} />}</span>
        </div>
        {open && <div className="grp-body">{children}</div>}
      </div>
    )
  }
  const Row = ({ label, children }: { label: string; children: React.ReactNode }) => (
    <div className="f-row">
      <span className="f-lb">{label}</span>
      {children}
    </div>
  )

  return (
    <div className="space-y-3">
      <Grp id="engine" title={tr('引擎模式')} note="rule / llm / auto">
        <div className="mod-row">
          {(['auto', 'rule', 'llm'] as const).map((m) => (
            <button
              key={m}
              className={`mod-toggle ${cfg.engine.mode === m ? 'on' : ''}`}
              onClick={() => set('engine', 'mode', m)}
            >
              <Zap size={12} />
              {m === 'auto' ? '自动（有Key走LLM）' : m === 'llm' ? '强制LLM' : '纯规则'}
            </button>
          ))}
        </div>
        <Row label={tr('说明')}>
          <span className="text-xs text-muted">
            {cfg.engine.mode === 'llm' && '强制 LLM 模式，无 Key 时自动降级并标注 rule_fallback。'}
            {cfg.engine.mode === 'rule' && '纯规则 ReAct，不依赖 LLM。'}
            {cfg.engine.mode === 'auto' && '有 LLM Key 走 LLM，否则规则兜底。'}
          </span>
        </Row>
      </Grp>

      <Grp id="llm" title={tr('模型 / LLM')} note="密钥可在界面配置（存本机库）或由环境变量注入">
        <Row label={tr('API Key 状态')}>
          <span className={`badge ${cfg.llm.has_key ? 'ok' : 'muted'}`}>
            {cfg.llm.has_key ? '已配置 · ' + cfg.llm.model : '未配置'}
            {cfg.llm.key_source === 'ui' ? ' · 界面' : cfg.llm.key_source === 'env' ? ' · 环境变量' : ''}
          </span>
        </Row>
        <Row label="API Key">
          <div className="flex items-center gap-2 flex-wrap">
            <input
              type="password"
              placeholder={cfg.llm.api_key ? `已配置（${cfg.llm.api_key}）` : 'sk-...'}
              value={apiKeyInput}
              onChange={(e) => setApiKeyInput(e.target.value)}
              style={{ flex: 2, minWidth: 180 }}
            />
            <button className="btn sm" onClick={() => { set('llm', 'api_key', apiKeyInput); setApiKeyInput('') }} disabled={!apiKeyInput}>
              <Save size={13} /> 保存
            </button>
          </div>
        </Row>
        <Row label="Base URL"><input type="text" value={cfg.llm.base_url} onChange={(e) => set('llm', 'base_url', e.target.value)} /></Row>
        <Row label="Model">
          {modelsLoaded ? (
            <select value={cfg.llm.model} onChange={(e) => set('llm', 'model', e.target.value)} style={{ flex: 2, minWidth: 180, border: '1px solid var(--line)', borderRadius: 'var(--r-sm)', padding: '7px 12px', fontSize: 13, fontFamily: 'var(--font)', background: 'var(--panel)' }}>
              {modelsRes!.models.map((m) => (
                <option key={m} value={m}>{m}</option>
              ))}
            </select>
          ) : (
            <input type="text" value={cfg.llm.model} onChange={(e) => set('llm', 'model', e.target.value)} />
          )}
        </Row>
        <Row label={tr('连通 / 模型列表')}>
          <div className="flex items-center gap-2 flex-wrap">
            <button className="btn sm" onClick={() => llmTest.mutate()} disabled={llmTest.isPending}>
              <Zap size={13} /> {llmTest.isPending ? '测试中…' : '测试连通'}
            </button>
            <button className="btn secondary sm" onClick={() => llmModels.mutate()} disabled={llmModels.isPending}>
              <Search size={13} /> {llmModels.isPending ? '获取中…' : '可用模型'}
            </button>
            {llmTest.data && (
              <span className={`badge ${llmTest.data.ok ? 'ok' : 'danger'}`} title={llmTest.data.reply || ''}>
                {llmTest.data.ok ? `连通 OK · ${llmTest.data.latency_ms}ms · ${llmTest.data.model}` : (llmTest.data.error || '失败')}
              </span>
            )}
            {llmModels.data && llmModels.data.ok && (
              <span className="badge info">可用模型 {llmModels.data.count} 个</span>
            )}
            {llmModels.data && !llmModels.data.ok && llmModels.data.error && (
              <span className="badge danger">{llmModels.data.error}</span>
            )}
          </div>
        </Row>
        <Row label={`Temperature · ${cfg.llm.temperature}`}>
          <input type="range" min="0" max="2" step="0.1" value={cfg.llm.temperature} onChange={(e) => set('llm', 'temperature', parseFloat(e.target.value))} />
        </Row>
        <Row label={`Top-p · ${cfg.llm.top_p}`}>
          <input type="range" min="0" max="1" step="0.05" value={cfg.llm.top_p} onChange={(e) => set('llm', 'top_p', parseFloat(e.target.value))} />
        </Row>
        <Row label={`Max tokens · ${cfg.llm.max_tokens}`}>
          <input type="range" min="128" max="4096" step="128" value={cfg.llm.max_tokens} onChange={(e) => set('llm', 'max_tokens', parseInt(e.target.value, 10))} />
        </Row>
        <Row label={tr('备援模型')}>
          <input
            type="text"
            title={tr('主模型请求失败时自动换用（如 glm-4-flash ↔ deepseek-chat），留空不启用')}
            placeholder={tr('如 glm-4-flash，留空=不启用')}
            value={cfg.llm.fallback_model ?? ''}
            onChange={(e) => set('llm', 'fallback_model', e.target.value.trim())}
            style={{ maxWidth: 260 }}
          />
        </Row>
      </Grp>

      <Grp id="retrieval" title={tr('知识检索（RAG）')}>
        <Row label={`Top-K · ${cfg.retrieval.kb_top_k}`}>
          <input type="range" min="1" max="10" value={cfg.retrieval.kb_top_k} onChange={(e) => set('retrieval', 'kb_top_k', parseInt(e.target.value, 10))} />
        </Row>
        <Row label={`最低重叠 · ${cfg.retrieval.min_overlap}`}>
          <input type="range" min="0" max="0.3" step="0.01" value={cfg.retrieval.min_overlap} onChange={(e) => set('retrieval', 'min_overlap', parseFloat(e.target.value))} />
        </Row>
        <Row label={tr('向量语义检索')}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
            <select
              value={cfg.retrieval.embedding || 'auto'}
              onChange={(e) => set('retrieval', 'embedding', e.target.value)}
              style={{ minWidth: 150 }}
            >
              <option value="auto">{tr('自动（可用即用）')}</option>
              <option value="off">{tr('关闭（纯字面）')}</option>
            </select>
            <span className="text-xs" style={{ color: 'var(--muted)' }}>
              {kbMeta?.embedding?.available
                ? `BGE 已就绪（${kbMeta.embedding.model}），与字面检索混合评分`
                : '未安装 sentence-transformers，当前纯字面检索（pip install -r requirements-embedding.txt）'}
            </span>
          </div>
        </Row>
      </Grp>

      <Grp id="context" title={tr('上下文工程')} note="相关性筛选 + 摘要 + 字符预算 + 模块开关">
        <Row label={`历史轮数 · ${cfg.context.history_turns}`}>
          <input type="range" min="0" max="30" value={cfg.context.history_turns} onChange={(e) => set('context', 'history_turns', parseInt(e.target.value, 10))} />
        </Row>
        <Row label={`字符预算 · ${cfg.context.char_budget}`}>
          <input type="range" min="500" max="8000" step="100" value={cfg.context.char_budget} onChange={(e) => set('context', 'char_budget', parseInt(e.target.value, 10))} />
        </Row>
        <Row label={`压缩阈值 · ${cfg.context.compress_threshold}`}>
          <input type="range" min="2" max="20" value={cfg.context.compress_threshold} onChange={(e) => set('context', 'compress_threshold', parseInt(e.target.value, 10))} />
        </Row>
        <div className="f-row">
          <span className="f-lb">{tr('选项')}</span>
          <label className="f-row" style={{ padding: 0 }}>
            <input type="checkbox" checked={cfg.context.relevant_only} onChange={(e) => set('context', 'relevant_only', e.target.checked)} />
            <span className="text-xs">{tr('相关性筛选历史')}</span>
          </label>
          <label className="f-row" style={{ padding: 0 }}>
            <input type="checkbox" checked={cfg.context.summarize} onChange={(e) => set('context', 'summarize', e.target.checked)} />
            <span className="text-xs">{tr('长历史摘要')}</span>
          </label>
        </div>
        <Row label={tr('人设')}><textarea rows={2} value={cfg.context.persona} onChange={(e) => set('context', 'persona', e.target.value)} /></Row>
        <div className="mod-row">
          {Object.keys(cfg.context.modules).map((m) => (
            <label key={m} className={`mod-toggle ${cfg.context.modules[m] ? 'on' : ''}`}>
              <input type="checkbox" checked={!!cfg.context.modules[m]} onChange={(e) => set('context', 'modules', { ...cfg.context.modules, [m]: e.target.checked })} />
              {m}
            </label>
          ))}
        </div>
      </Grp>

      <Grp id="memory" title={tr('长期记忆')}>
        <div className="f-row">
          <span className="f-lb">{tr('选项')}</span>
          <label className="f-row" style={{ padding: 0 }}>
            <input type="checkbox" checked={cfg.memory.enable} onChange={(e) => set('memory', 'enable', e.target.checked)} />
            <span className="text-xs">{tr('启用跨会话记忆')}</span>
          </label>
          <label className="f-row" style={{ padding: 0 }}>
            <input type="checkbox" checked={cfg.memory.auto_capture} onChange={(e) => set('memory', 'auto_capture', e.target.checked)} />
            <span className="text-xs">{tr('自动沉淀偏好')}</span>
          </label>
          <label className="f-row" style={{ padding: 0 }}>
            <input type="checkbox" checked={cfg.memory.visit_greeting} onChange={(e) => set('memory', 'visit_greeting', e.target.checked)} />
            <span className="text-xs">{tr('老用户问候')}</span>
          </label>
        </div>
        <Row label={`相关记忆条数 · ${cfg.memory.retrieval_top_k}`}>
          <input type="range" min="1" max="10" value={cfg.memory.retrieval_top_k} onChange={(e) => set('memory', 'retrieval_top_k', parseInt(e.target.value, 10))} />
        </Row>
        <Row label={tr('偏好触发词')}><input type="text" value={cfg.memory.capture_keywords.join(',')} onChange={(e) => set('memory', 'capture_keywords', e.target.value.split(',').map((s) => s.trim()).filter(Boolean))} /></Row>
      </Grp>

      <Grp id="tools" title={tr('工具 / 多模态 / 护栏 / 升级')}>
        {Object.keys(cfg.tools).map((t) => (
          <div key={t} className="f-row">
            <span className="f-lb">{t}</span>
            <input type="checkbox" checked={!!cfg.tools[t]} onChange={(e) => set('tools', t, e.target.checked)} />
          </div>
        ))}
        <div className="f-row">
          <span className="f-lb">{tr('多模态（图片）')}</span>
          <input type="checkbox" checked={cfg.multimodal.enable} onChange={(e) => set('multimodal', 'enable', e.target.checked)} />
        </div>
        <div className="f-row">
          <span className="f-lb" title={tr('填视觉模型名后，用户发图走 LLM 看图；留空则请用户改用文字描述')}>{tr('视觉模型')}</span>
          <input
            type="text"
            placeholder={tr('如 glm-4v-flash，留空=请用户改用文字描述')}
            value={cfg.multimodal.vision_model ?? ''}
            onChange={(e) => set('multimodal', 'vision_model', e.target.value.trim())}
            style={{ maxWidth: 260 }}
          />
        </div>
        <div className="f-row">
          <span className="f-lb">{tr('注入护栏')}</span>
          <input type="checkbox" checked={cfg.guardrail.injection_block} onChange={(e) => set('guardrail', 'injection_block', e.target.checked)} />
        </div>
        <Row label={`升级置信阈值 · ${cfg.escalation.confidence_threshold}`}>
          <input type="range" min="0.1" max="1" step="0.05" value={cfg.escalation.confidence_threshold} onChange={(e) => set('escalation', 'confidence_threshold', parseFloat(e.target.value))} />
        </Row>
        <Row label={`消息长度上限 · ${cfg.escalation.max_message_len}`}>
          <input type="range" min="200" max="5000" step="100" value={cfg.escalation.max_message_len} onChange={(e) => set('escalation', 'max_message_len', parseInt(e.target.value, 10))} />
        </Row>
        <Row label={`ReAct 步数 · ${cfg.escalation.react_steps}`}>
          <input type="range" min="1" max="8" value={cfg.escalation.react_steps} onChange={(e) => set('escalation', 'react_steps', parseInt(e.target.value, 10))} />
        </Row>
        <div className="f-row">
          <span className="f-lb" title={tr('关：纯机器人，对客说「转人工」时如实告知没有真人；开：会话进入待接管队列，需有人在协作台值守')}>{tr('真人接管')}</span>
          <input type="checkbox" checked={!!cfg.escalation.handoff}
            onChange={(e) => set('escalation', 'handoff', e.target.checked)} />
        </div>
      </Grp>
    </div>
  )
}

/* ============ 工具注册表（可自定义内部功能） ============ */
function ToolsPanel() {
  const { data: toolsRes, isLoading, refetch } = useTools()
  const tm = useToolMutations()
  const fb = useFeedback()
  const [editing, setEditing] = useState<ToolDef | 'new' | null>(null)
  const [newArgs, setNewArgs] = useState('')

  if (isLoading) return <Spinner full label={tr('加载工具注册表…')} />
  const tools = toolsRes?.tools ?? []

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <span className="text-xs text-muted">{tr('工具数统计', { n: tools.length })}</span>
        <button className="btn sm" onClick={() => setEditing('new')}><Plus size={14} /> {tr('新增工具')}</button>
      </div>

      <div className="kb-grid">
        {tools.map((t) => (
          <div className={`kb-card tool-card ${!t.enabled ? 'opacity-55' : ''}`} key={t.id}>
            <div className="kc-head">
              <span className="kc-id">{t.id}</span>
              <span className="kc-title">{t.name}</span>
              <div className="kc-actions" style={{ opacity: 1 }}>
                <button className="btn ghost sm" title={tr('编辑')} onClick={() => setEditing(t)}><Pencil size={13} /></button>
                <button className="btn ghost sm" title={t.enabled ? '停用' : '启用'} style={{ color: t.enabled ? 'var(--muted)' : 'var(--ok)' }} onClick={() => tm.toggle.mutate({ id: t.id, enabled: !t.enabled })}>
                  <Activity size={13} /> {t.enabled ? '停用' : '启用'}
                </button>
                <button className="btn ghost sm" title={tr('删除')} style={{ color: 'var(--danger)' }} onClick={async () => {
                  const ok = await fb.confirm(`删除工具 ${t.id}？删除后 Agent 将不再具备该能力。`, { confirmText: '删除' })
                  if (ok) tm.remove.mutate(t.id)
                }}><Trash2 size={13} /></button>
              </div>
            </div>
            <div className="kc-tags">
              <em>{t.builtin ? '内置' : '自定义'}</em>
              <em>{JSON.stringify(t.args_json)}</em>
            </div>
            <p className="kc-answer">{t.desc || '（无描述）'}</p>
          </div>
        ))}
      </div>

      {editing && (
        <div className="drawer-mask" onClick={() => setEditing(null)}>
          <div className="drawer" onClick={(e) => e.stopPropagation()}>
            <div className="drawer-head">
              <h3>{editing === 'new' ? '新增工具' : '编辑 ' + (editing as ToolDef).id}</h3>
              <button className="btn ghost sm" aria-label={tr('关闭')} title={tr('关闭')} onClick={() => setEditing(null)}><X size={14} /></button>
            </div>
            <div className="drawer-body">
              <ToolForm
                key={editing === 'new' ? 'new' : (editing as ToolDef).id}
                initial={editing === 'new' ? null : (editing as ToolDef)}
                newArgs={newArgs}
                setNewArgs={setNewArgs}
                isNew={editing === 'new'}
                onDone={() => setEditing(null)}
              />
            </div>
          </div>
        </div>
      )}
      {tm.create.isError && <ErrorBanner message={(tm.create.error as Error).message} onRetry={() => refetch()} />}
    </div>
  )
}

function ToolForm({ initial, isNew, onDone, newArgs, setNewArgs }: {
  initial: ToolDef | null; isNew: boolean; onDone: () => void
  newArgs: string; setNewArgs: (s: string) => void
}) {
  const tm = useToolMutations()
  const fb = useFeedback()
  const [id, setId] = useState(initial?.id ?? '')
  const [name, setName] = useState(initial?.name ?? '')
  const [desc, setDesc] = useState(initial?.desc ?? '')
  const [argsText, setArgsText] = useState(isNew ? newArgs : JSON.stringify(initial?.args_json ?? {}, null, 2))

  const save = () => {
    let args: Record<string, string> = {}
    try {
      args = JSON.parse(argsText || '{}')
    } catch {
      fb.toast.error('args_json 不是合法 JSON')
      return
    }
    if (!id.trim()) return
    const payload: ToolDef = { id: id.trim(), name: name || id.trim(), desc, args_json: args, enabled: initial?.enabled ?? true, builtin: initial?.builtin ?? false }
    const p = isNew ? tm.create.mutateAsync(payload) : tm.update.mutateAsync(payload)
    p.then(onDone).catch((e) => fb.toast.error((e as Error).message))
  }

  return (
    <>
      <div className="d-field">
        <label>{tr('工具 ID（唯一）')}</label>
        <input value={id} disabled={!isNew} onChange={(e) => setId(e.target.value)} placeholder={tr('如 kb_faq')} />
      </div>
      <div className="d-field">
        <label>{tr('名称')}</label>
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder={tr('查常见问题')} />
      </div>
      <div className="d-field">
        <label>{tr('描述（供 LLM 理解）')}</label>
        <textarea rows={2} value={desc} onChange={(e) => setDesc(e.target.value)} placeholder={tr('按主题检索 FAQ 知识库')} />
      </div>
      <div className="d-field">
        <label>{tr('参数 schema（JSON：参数名 → 类型）')}</label>
        <textarea rows={3} value={argsText} onChange={(e) => { setArgsText(e.target.value); setNewArgs(e.target.value) }} placeholder={'{"topic": "string"}'} />
      </div>
      <div className="drawer-foot">
        <button className="btn secondary" onClick={onDone}>{tr('取消')}</button>
        <button className="btn" onClick={save} disabled={!id.trim()}><Save size={14} /> {tr('保存')}</button>
      </div>
    </>
  )
}

/* ============ 注入规则管理 ============ */
function GuardrailPanel() {
  const fb = useFeedback()
  const { data: rulesRes, isLoading, refetch } = useInjectionRules()
  const rm = useInjectionRuleMutations()
  const [expr, setExpr] = useState('')
  const [action, setAction] = useState<'block' | 'log_only'>('block')

  if (isLoading) return <Spinner full label={tr('加载注入规则…')} />
  const rules = rulesRes?.rules ?? []

  const add = () => {
    if (!expr.trim()) return
    rm.create.mutate({ expr: expr.trim(), action, enabled: true }, {
      onSuccess: () => setExpr(''),
      onError: (e) => fb.toast.error((e as Error).message),
    })
  }

  return (
    <div>
      <div className="grp" style={{ marginBottom: 16 }}>
        <div className="grp-head"><h3>{tr('新增规则')}</h3></div>
        <div className="grp-body">
          <div className="f-row">
            <span className="f-lb">{tr('正则 / 关键词')}</span>
            <input type="text" value={expr} onChange={(e) => setExpr(e.target.value)} placeholder='如 (pretend|假装)\s*你\s*是' style={{ flex: 2 }} />
          </div>
          <div className="f-row">
            <span className="f-lb">{tr('动作')}</span>
            <div className="flex gap-2">
              <button className={`mod-toggle ${action === 'block' ? 'on' : ''}`} onClick={() => setAction('block')}>{tr('拦截')}</button>
              <button className={`mod-toggle ${action === 'log_only' ? 'on' : ''}`} onClick={() => setAction('log_only')}>{tr('仅记录')}</button>
            </div>
          </div>
          <div className="f-row">
            <button className="btn sm" onClick={add} disabled={!expr.trim()}><Plus size={13} /> {tr('添加规则')}</button>
            {rm.create.isError && <span className="text-xs text-[#c93034]">{(rm.create.error as Error).message}</span>}
          </div>
        </div>
      </div>

      <div className="tbl-wrap">
        <table className="dt">
          <thead><tr><th>#</th><th>{tr('表达式')}</th><th>{tr('动作')}</th><th>{tr('启用')}</th><th></th></tr></thead>
          <tbody>
            {rules.map((r) => (
              <tr key={r.id}>
                <td>{r.id}</td>
                <td className="raw" title={r.expr}>{r.expr}</td>
                <td>
                  <button className={`badge ${r.action === 'block' ? 'danger' : 'warn'}`} onClick={() => rm.update.mutate({ ...r, action: r.action === 'block' ? 'log_only' : 'block' })}>
                    {r.action === 'block' ? '拦截' : '仅记录'}
                  </button>
                </td>
                <td>
                  <input type="checkbox" checked={r.enabled} onChange={(e) => rm.update.mutate({ ...r, enabled: e.target.checked })} />
                </td>
                <td>
                  <button className="btn ghost sm" aria-label={tr('删除规则')} title={tr('删除规则')} onClick={() => rm.remove.mutate(r.id)}><Trash2 size={13} /></button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {rm.create.isError || rm.update.isError ? <div className="mt-2"><ErrorBanner message={(rm.create.error || rm.update.error || new Error()).message} onRetry={() => refetch()} /></div> : null}
    </div>
  )
}

/* ============ 知识库管理 ============ */
function KbPanel() {
  const fb = useFeedback()
  const { data: kbRes, isLoading } = useKb()
  const km = useKbMutations()
  const kbIo = useKbImport()
  const docIo = useKbDocImport()
  const kbTest = useKbTest()
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  const [query, setQuery] = useState('')
  const [recallQuery, setRecallQuery] = useState('')
  const [editing, setEditing] = useState<KbEntry | 'new' | null>(null)
  const [importing, setImporting] = useState(false)
  const [importText, setImportText] = useState('')
  const [importReplace, setImportReplace] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)

  const doImport = () => {
    kbIo.mutate(
      { text: importText, replace: importReplace },
      {
        onSuccess: (d) => {
          fb.toast.success(tr('导入成功统计', { added: d.added, total: d.total }))
          setImporting(false)
          setImportText('')
          setImportReplace(false)
        },
        onError: (e) => fb.toast.error((e as Error).message),
      },
    )
  }

  if (isLoading) return <Spinner full label={tr('加载知识库…')} />
  const all = kbRes?.kb ?? []
  const q = query.toLowerCase()
  const kb = all.filter((e) =>
    !q || e.id.toLowerCase().includes(q) || e.title.toLowerCase().includes(q) ||
    e.text.toLowerCase().includes(q) || (e.tags || []).some((t) => t.toLowerCase().includes(q)),
  )

  return (
    <>
      <div className="kb-search">
        <div style={{ position: 'relative', flex: 1 }}>
          <Search size={15} style={{ position: 'absolute', left: 16, top: '50%', transform: 'translateY(-50%)', color: 'var(--muted)' }} />
          <input style={{ paddingLeft: 40 }} placeholder={tr('搜索 id / 标题 / 关键词…')} value={query} onChange={(e) => setQuery(e.target.value)} />
        </div>
        {kbRes?.embedding && (
          <span
            className={`badge ${kbRes.embedding.available && kbRes.embedding.enabled !== 'off' ? 'ok' : 'info'}`}
            title={kbRes.embedding.available
              ? `向量后端 ${kbRes.embedding.model}：字面+语义混合检索`
              : '未安装 sentence-transformers，当前纯字面检索（可选装 requirements-embedding.txt）'}
          >
            {kbRes.embedding.enabled === 'off' ? '检索：纯字面（已关）' : kbRes.embedding.available ? '检索：字面+向量' : '检索：纯字面'}
          </span>
        )}
        <button className="btn secondary sm" title={tr('批量导入 Markdown')} onClick={() => setImporting(true)}><Upload size={13} /> {tr('导入')}</button>
        <button
          className="btn secondary sm"
          title={tr('上传 PDF / Word / Markdown / TXT，自动解析分块入库')}
          onClick={() => fileRef.current?.click()}
          disabled={docIo.isPending}
        >
          <Upload size={13} /> {docIo.isPending ? '解析中…' : '传文档'}
        </button>
        <input
          ref={fileRef}
          type="file"
          accept=".md,.markdown,.txt,.pdf,.docx,.xlsx"
          style={{ display: 'none' }}
          onChange={(e) => {
            const f = e.target.files?.[0]
            if (!f) return
            docIo.mutate(f, {
              onSuccess: (d: { file: string; added: number; total: number }) => fb.toast.success(tr('文档导入统计', { file: d.file, added: d.added, total: d.total })),
              onError: (err: unknown) => fb.toast.error((err as Error).message),
            })
            e.target.value = ''
          }}
        />
        <button className="btn secondary sm" title={tr('导出全部条目为 Markdown')} onClick={() => exportKb().catch((e) => fb.toast.error((e as Error).message))}><Download size={13} /> {tr('导出')}</button>
        <button className="btn" onClick={() => setEditing('new')}><Plus size={14} /> {tr('新增条目')}</button>
      </div>

      {/* 试召回：按当前生效配置跑一次检索，调库调参的试错工具 */}
      <div className="kb-search" style={{ marginBottom: 10 }}>
        <input
          placeholder={tr('试召回：输入用户可能的问法，看命中条目与分数…')}
          value={recallQuery}
          onChange={(e) => setRecallQuery(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && recallQuery.trim()) kbTest.mutate(recallQuery.trim()) }}
        />
        <button className="btn secondary sm" disabled={!recallQuery.trim() || kbTest.isPending} onClick={() => kbTest.mutate(recallQuery.trim())}>
          <Search size={13} /> {kbTest.isPending ? '检索中…' : '试召回'}
        </button>
      </div>
      {kbTest.isError && (
        <div style={{ color: 'var(--danger)', marginBottom: 12, fontSize: 13 }}>{(kbTest.error as Error).message}</div>
      )}
      {kbTest.data && (
        <div style={{ marginBottom: 12, fontSize: 13 }}>
          <div style={{ color: 'var(--muted)', marginBottom: 6 }}>
            top-K {kbTest.data.top_k} · 门槛 {kbTest.data.min_overlap} · 向量 {kbTest.data.embedding}
          </div>
          {kbTest.data.hits.length === 0 && <div style={{ color: 'var(--muted)' }}>{tr('无命中（会走兜底话术）')}</div>}
          {kbTest.data.hits.map((h: { id: string; title: string; score: number; answer: string }, i: number) => (
            <div key={h.id} className="f-row" style={{ justifyContent: 'flex-start', gap: 8, padding: '4px 0' }}>
              <span className={`badge ${i === 0 && h.score >= 0.3 ? 'ok' : 'info'}`}>{h.score.toFixed(2)}</span>
              <span style={{ fontFamily: 'monospace', fontSize: 12 }}>{h.id}</span>
              <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={h.answer}>{h.title}</span>
            </div>
          ))}
        </div>
      )}

      {kb.length === 0 && <EmptyState icon={<BookOpen size={22} />} title={tr('没有匹配的知识库条目')} desc={tr('调整搜索词或新增一条')} />}

      <div className="kb-grid">
        {kb.map((e) => (
          <div className="kb-card" key={e.id}>
            <div className="kc-head">
              <span className="kc-id">{e.id}</span>
              <span className="kc-title">{e.title}</span>
              <div className="kc-actions" style={{ opacity: 1 }}>
                <button className="btn ghost sm" aria-label={tr('编辑条目')} title={tr('编辑条目')} onClick={() => setEditing(e)}><Pencil size={13} /></button>
                <button className="btn ghost sm" aria-label={tr('删除条目')} title={tr('删除条目')} style={{ color: 'var(--danger)' }} onClick={async () => {
                  const ok = await fb.confirm(`删除知识库条目 ${e.id}？删除后相关问答将失去依据。`, { confirmText: '删除' })
                  if (ok) km.remove.mutate(e.id)
                }}><Trash2 size={13} /></button>
              </div>
            </div>
            <div className="kc-tags">{(e.tags || []).map((t) => <em key={t}>{t}</em>)}</div>
            <p className="kc-text">{e.text}</p>
            <p className="kc-answer">{e.answer}</p>
          </div>
        ))}
      </div>

      {editing && (
        <div className="drawer-mask" onClick={() => setEditing(null)}>
          <div className="drawer" onClick={(ev) => ev.stopPropagation()}>
            <div className="drawer-head">
              <h3>{editing === 'new' ? '新增知识库条目' : '编辑 ' + (editing as KbEntry).id}</h3>
              <button className="btn ghost sm" onClick={() => setEditing(null)}><X size={14} /></button>
            </div>
            <div className="drawer-body">
              <KbForm key={editing === 'new' ? 'new' : (editing as KbEntry).id} initial={editing === 'new' ? null : (editing as KbEntry)} onDone={() => setEditing(null)} />
            </div>
          </div>
        </div>
      )}

      {importing && (
        <div className="drawer-mask" onClick={() => setImporting(false)}>
          <div className="drawer" onClick={(ev) => ev.stopPropagation()}>
            <div className="drawer-head">
              <h3>{tr('批量导入知识库')}</h3>
              <button className="btn ghost sm" onClick={() => setImporting(false)}><X size={14} /></button>
            </div>
            <div className="drawer-body">
              <div className="d-field">
                <label>{tr('支持的格式（自动识别）')}</label>
                <span className="text-xs" style={{ color: 'var(--muted)' }}>
                  ① 每条以 <code>{tr('## 标题')}</code> 开头，其后为答案（可含空行）；② 空行分隔的块，首行=标题，其余=答案
                </span>
              </div>
              <div className="d-field">
                <label>{tr('粘贴文本，或选择 .md / .txt 文件')}</label>
                <textarea
                  rows={10}
                  value={importText}
                  onChange={(e) => setImportText(e.target.value)}
                  placeholder={'## 退货政策\n签收后 7 天内支持无理由退货退款…\n\n## 发货时效\n付款后 48 小时内发出'}
                />
                <input type="file" accept=".md,.txt,text/plain,text/markdown" style={{ marginTop: 8 }} onChange={(e) => {
                  const f = e.target.files?.[0]
                  if (f) f.text().then((t) => setImportText(t))
                }} />
              </div>
              <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer' }}>
                <input type="checkbox" checked={importReplace} onChange={(e) => setImportReplace(e.target.checked)} />
                <span className="text-xs" style={{ color: 'var(--danger)' }}>{tr('清空现有条目后导入（整库重建，谨慎使用）')}</span>
              </label>
              <div className="drawer-foot">
                <button className="btn secondary" onClick={() => setImporting(false)}>{tr('取消')}</button>
                <button className="btn" onClick={doImport} disabled={!importText.trim() || kbIo.isPending}>
                  <Upload size={14} /> {kbIo.isPending ? '导入中…' : '开始导入'}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </>
  )
}

function KbForm({ initial, onDone }: { initial: KbEntry | null; onDone: () => void }) {
  const km = useKbMutations()
  const fb = useFeedback()
  const [id, setId] = useState(initial?.id ?? '')
  const [title, setTitle] = useState(initial?.title ?? '')
  const [text, setText] = useState(initial?.text ?? '')
  const [answer, setAnswer] = useState(initial?.answer ?? '')
  const [tags, setTags] = useState(initial?.tags.join(',') ?? '')
  const isNew = !initial

  const save = () => {
    const e: KbEntry = { id: id.trim(), title, text, answer, tags: tags.split(',').map((s: string) => s.trim()).filter((s: string) => s.length > 0) }
    const p = isNew ? km.create.mutateAsync(e) : km.update.mutateAsync(e)
    p.then(onDone).catch((err) => fb.toast.error((err as Error).message))
  }

  return (
    <>
      <div className="d-field"><label>{tr('条目 ID')}</label><input value={id} disabled={!isNew} onChange={(ev) => setId(ev.target.value)} placeholder="KB007" /></div>
      <div className="d-field"><label>{tr('标题')}</label><input value={title} onChange={(ev) => setTitle(ev.target.value)} /></div>
      <div className="d-field"><label>{tr('检索关键词（空格分隔）')}</label><input value={text} onChange={(ev) => setText(ev.target.value)} /></div>
      <div className="d-field"><label>{tr('答案')}</label><textarea rows={4} value={answer} onChange={(ev) => setAnswer(ev.target.value)} /></div>
      <div className="d-field"><label>{tr('标签（逗号分隔）')}</label><input value={tags} onChange={(ev) => setTags(ev.target.value)} /></div>
      <div className="drawer-foot">
        <button className="btn secondary" onClick={onDone}>{tr('取消')}</button>
        <button className="btn" onClick={save} disabled={!id.trim() || !title.trim()}><Save size={14} /> {tr('保存')}</button>
      </div>
    </>
  )
}

/* ============ 数据概览 ============ */
function DataPanel() {
  const { data: ov, isLoading } = useAdminOverview()
  const { data: report } = useAdminReport()
  const { data: stats } = useAdminStats()
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

function RepItem({ label, value, sub, tone = 'brand' }: { label: string; value: string | number; sub: string; tone?: string }) {
  const color = tone === 'ok' ? '#1a8f52' : tone === 'warn' ? '#b5731b' : tone === 'muted' ? 'var(--muted)' : 'var(--brand)'
  return (
    <div className="rep-item">
      <span className="rep-label">{label}</span>
      <span className="rep-value" style={{ color }}>{value}</span>
      <span className="rep-sub">{sub}</span>
    </div>
  )
}

function Pager({ page, maxPage, total, onChange, label }: { page: number; maxPage: number; total: number; onChange: (p: number) => void; label: string }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 12, fontSize: 12, color: 'var(--muted)' }}>
      <span>{tr('分页状态', { total, page: page + 1, pages: maxPage + 1 })}</span>
      <button className="btn ghost sm" disabled={page === 0} onClick={() => onChange(page - 1)}><ChevronLeft size={14} /></button>
      <button className="btn ghost sm" disabled={page >= maxPage} onClick={() => onChange(page + 1)}><ChevronRight size={14} /></button>
    </div>
  )
}

/* ============ 长期记忆 ============ */
function MemPanel() {
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
function InjPanel() {
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


/* ============ 访问令牌（本机自动获取 + 可视化复制） ============ */
function TokenPanel() {
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
