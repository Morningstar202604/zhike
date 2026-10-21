import React from 'react'
import {
  Sparkles,
  Paperclip,
  ImagePlus,
  Send,
  ThumbsUp,
  ThumbsDown,
  ShieldCheck,
  UserRound,
  CircleCheck,
  X,
  RefreshCw,
  Globe,
  Square,
  Brain,
  ChevronRight,
} from 'lucide-react'

export function Avatar({ kind = 'agent', size = 34 }) {
  const map = {
    agent: { icon: <Sparkles size={size * 0.5} />, bg: 'var(--brand-grad)', color: '#fff', label: 'Agent' },
    visitor: { icon: <UserRound size={size * 0.5} />, bg: 'var(--visitor-soft)', color: 'var(--brand)', label: '访客' },
    operator: { icon: <CircleCheck size={size * 0.5} />, bg: 'var(--operator-soft)', color: '#c2358f', label: '坐席' },
  }
  const m = map[kind] || map.agent
  return (
    <span
      style={{
        width: size,
        height: size,
        borderRadius: '50%',
        background: m.bg,
        color: m.color,
        display: 'grid',
        placeItems: 'center',
        flexShrink: 0,
        boxShadow: kind === 'agent' ? '0 4px 10px rgba(43,92,255,0.25)' : 'none',
      }}
      title={m.label}
    >
      {m.icon}
    </span>
  )
}

const TOOL_LABEL = {
  query_order: '查订单',
  initiate_refund: '退款申请',
  lookup_policy: '查政策',
  vision: '图片识别',
  injection_guard: '安全护栏',
}
export function toolLabel(name) {
  return TOOL_LABEL[name] || name
}

const MODE_LABEL = {
  rule_react: '规则引擎',
  rule_fallback: '规则兜底',
  llm_react: 'LLM',
  guardrail: '安全拦截',
  fallback: '异常兜底',
}

export function MsgMeta({ msg }) {
  if (!msg) return null
  const chips = []
  if (msg.refs && msg.refs.length) chips.push(`依据 ${msg.refs.join(' / ')}`)
  // injection_guard / feedback 是内部流程标记，不是用户可感知的工具
  if (msg.tool_used && !['injection_guard', 'feedback', 'vision'].includes(msg.tool_used)) {
    chips.push(`工具：${toolLabel(msg.tool_used)}`)
  }
  if (msg.ctx_used) {
    const c = msg.ctx_used
    if (c.kb && c.kb.hits && c.kb.hits.length) chips.push(`KB×${c.kb.hits.length}`)
    if (c.memory && c.memory.hits && c.memory.hits.length) chips.push(`记忆×${c.memory.hits.length}`)
    if (c.web && c.web.hits) chips.push(`联网×${c.web.hits}`)
    if (c.history && c.history.summarized) chips.push('历史摘要')
  }
  // 引擎模式与耗时：Codex 会显示这类运行信息，便于用户判断回答来源
  if (msg.mode) chips.push(MODE_LABEL[msg.mode] || msg.mode)
  if (msg.latency_ms) chips.push(`${(msg.latency_ms / 1000).toFixed(1)}s`)
  if (!chips.length) return null
  return (
    <span className="msg-meta">
      {chips.map((c, i) => (
        <em key={i}>{c}</em>
      ))}
    </span>
  )
}

export function FeedbackRow({ onFeedback, feedback, show }) {
  if (!show) return null
  return (
    <div className="feedback-row">
      <span className="feedback-label">这条回答有用吗？</span>
      <button className={`fb-btn ${feedback === 'helpful' ? 'on' : ''}`} onClick={() => onFeedback('helpful')}>
        <ThumbsUp size={13} />
        {feedback === 'helpful' ? '已标记有用' : '有用'}
      </button>
      <button className={`fb-btn ${feedback === 'not_helpful' ? 'on' : ''}`} onClick={() => onFeedback('not_helpful')}>
        <ThumbsDown size={13} />
        {feedback === 'not_helpful' ? '已转人工' : '没用'}
      </button>
    </div>
  )
}

export function InjectBanner({ text, onClose }) {
  if (!text) return null
  return (
    <div className="inject-banner">
      <ShieldCheck size={15} />
      <span>{text}</span>
      {onClose && (
        <button className="inject-close" onClick={onClose}>
          <X size={13} />
        </button>
      )}
    </div>
  )
}

export function Composer({
  value,
  onChange,
  onSend,
  onPickImage,
  onRemoveImage,
  image,
  loading,
  maxLength = 1000,
  placeholder = '输入您的问题…',
  disabled,
  imageTip = '上传图片（多模态）',
  /** 联网搜索开关（可选）。传入则显示按钮；不传则不显示。 */
  webSearch = false,
  onToggleWebSearch,
  webSearchEnabled = false,
  /** 传入则在生成中显示「停止」按钮 */
  onStop,
  /** 未配置搜索服务时的说明；传入则显示禁用的「联网搜索」按钮 */
  webSearchHint,
}) {
  const ref = React.useRef(null)

  // 输入框随内容自动增高（最多 6 行），像主流 Agent 对话那样
  React.useEffect(() => {
    const el = ref.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = Math.min(el.scrollHeight, 132) + 'px'
  }, [value])

  const hasContent = !!(value.trim() || image)

  return (
    <div className="composer">
      <div className="composer-top">
        <label className="icon-btn" title={imageTip}>
          <ImagePlus size={16} />
          <input type="file" accept="image/*" style={{ display: 'none' }} onChange={onPickImage} />
        </label>
        {onToggleWebSearch && (
          <button
            type="button"
            className={`tool-toggle ${webSearchEnabled ? 'on' : ''}`}
            onClick={onToggleWebSearch}
            title={webSearchEnabled ? '联网搜索：已开启（回答会参考实时信息）' : '联网搜索：关闭'}
            aria-pressed={webSearchEnabled}
          >
            <Globe size={13} />
            <span>联网搜索</span>
          </button>
        )}
        {/* 未配置搜索服务：保留入口但禁用，并在 tooltip 里说清怎么开——直接隐藏会让用户
            以为没有这个能力，点开又只会得到「未配置」错误。 */}
        {!onToggleWebSearch && webSearchHint && (
          <button type="button" className="tool-toggle" disabled title={webSearchHint}>
            <Globe size={13} />
            <span>联网搜索</span>
          </button>
        )}
        {webSearch && <span className="badge info" style={{ fontSize: 10 }}>已开启</span>}
        {image && (
          <div className="composer-img">
            <img src={image} alt="预览" />
            {onRemoveImage && (
              <button title="移除图片" onClick={onRemoveImage}>
                <X size={11} />
              </button>
            )}
          </div>
        )}
      </div>
      <div className="composer-bar">
        <textarea
          ref={ref}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              onSend()
            }
            // Esc 清空输入，与主流编辑器一致
            if (e.key === 'Escape' && value) {
              e.preventDefault()
              onChange('')
            }
          }}
          placeholder={placeholder}
          maxLength={maxLength}
          rows={1}
          disabled={disabled}
          aria-label="消息输入框"
        />
        <span className="composer-count">
          {value.length}/{maxLength}
        </span>
        {loading && onStop ? (
          <button className="send-btn stop" onClick={onStop} title="停止生成">
            <Square size={13} />
            <span>停止</span>
          </button>
        ) : (
          <button
            className="send-btn"
            onClick={() => onSend()}
            disabled={loading || !hasContent || disabled}
            title="发送（Enter），换行用 Shift+Enter"
          >
            <Send size={15} />
            <span>发送</span>
          </button>
        )}
      </div>
      <div className="composer-hint">
        <kbd>Enter</kbd> 发送 · <kbd>Shift</kbd>+<kbd>Enter</kbd> 换行 · <kbd>Esc</kbd> 清空
        {onToggleWebSearch && webSearchEnabled && <span style={{ marginLeft: 8, color: 'var(--brand)' }}>· 联网搜索已开</span>}
      </div>
    </div>
  )
}

export function PageHead({ title, sub, right }) {
  return (
    <div className="page-head">
      <div>
        <h1 className="page-title">{title}</h1>
        {sub && <p className="page-sub">{sub}</p>}
      </div>
      {right && <div className="page-head-right">{right}</div>}
    </div>
  )
}

export function StatCard({ icon, value, label, tone = 'brand' }) {
  const tones = {
    brand: { bg: 'var(--brand-soft)', color: 'var(--brand)' },
    ok: { bg: 'var(--ok-soft)', color: '#1a8f52' },
    warn: { bg: 'var(--warn-soft)', color: '#b5731b' },
    danger: { bg: 'var(--danger-soft)', color: '#c93034' },
    muted: { bg: 'var(--system-soft)', color: 'var(--ink-2)' },
  }
  const t = tones[tone] || tones.brand
  return (
    <div className="stat">
      <div className="stat-icon" style={{ background: t.bg, color: t.color }}>
        {icon}
      </div>
      <div className="stat-val">{value}</div>
      <div className="stat-lb">{label}</div>
    </div>
  )
}

export function EmptyState({ icon, title, desc, action = null }) {
  return (
    <div className="empty-state">
      {icon && <div className="empty-icon">{icon}</div>}
      {title && <div className="empty-title">{title}</div>}
      {desc && <div className="empty-desc">{desc}</div>}
      {action}
    </div>
  )
}


/**
 * 思考过程折叠块。
 * 默认收起（避免长推理干扰阅读），点击展开看模型当时的推理。
 * 与 Codex 的 "Thought for Xs" 同构。
 */
export function ThoughtBlock({ text }) {
  const [open, setOpen] = React.useState(false)
  if (!text || !text.trim()) return null
  return (
    <div className="thought-block">
      <button
        type="button"
        className="thought-head"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
      >
        <Brain size={12} />
        <span>{open ? '隐藏思考过程' : '查看思考过程'}</span>
        <ChevronRight size={12} className={`chev ${open ? 'open' : ''}`} />
      </button>
      {open && <div className="thought-body">{text}</div>}
    </div>
  )
}
