import { useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { Save, RefreshCw, Plus, Pencil, Trash2, X, Wrench, ShieldAlert, ChevronDown, ChevronUp, Zap, Search, Activity, BookOpen } from 'lucide-react'
import { useKb, useLlmTest, useLlmModels, useTools, useToolMutations, useInjectionRules, useInjectionRuleMutations, useUpdateConfig } from '../lib/queries'
import { useFeedback } from '../components/Feedback'
import type { AgentConfig, ToolDef, InjectionRule } from '../types/api'
import { Spinner, ErrorBanner } from '../components/Spinner'
import { EmptyState } from '../ui'
import { t as tr } from '../lib/t'
import { Grp, Row } from './bits'

export function ParamsPanel({ cfg, setDraft, upd }: {
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
export function ToolsPanel() {
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

export function ToolForm({ initial, isNew, onDone, newArgs, setNewArgs }: {
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
export function GuardrailPanel() {
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
