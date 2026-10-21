import { useRef, useEffect, useState, useCallback, memo } from 'react'
import {
  Plus,
  RefreshCw,
  Package,
  Undo2,
  ScrollText,
  User,
  Headset,
  MessageCircle,
  ShieldCheck,
  Copy,
  Trash2,
} from 'lucide-react'
import {
  Avatar,
  Composer,
  FeedbackRow,
  InjectBanner,
  MsgMeta,
} from './ui'
import { useWs } from './hooks/useWs'
import { ensureVisitorId, useAppStore } from './store/appStore'
import Logo from './components/Logo'
import { useShell } from './components/ResponsiveShell'
import { http } from './lib/queries'
import './pages.css'
const SCENES = [
  { icon: <Package size={16} />, bg: 'var(--brand-soft)', color: 'var(--brand)', title: '查订单物流', text: '我的订单 ORD12345678 什么时候发货？' },
  { icon: <Undo2 size={16} />, bg: 'var(--ok-soft)', color: '#1a8f52', title: '申请退款', text: '我想申请退款，怎么操作？' },
  { icon: <ScrollText size={16} />, bg: 'var(--warn-soft)', color: '#b5731b', title: '查退改政策', text: '旅游团退改政策是怎么规定的？' },
  { icon: <User size={16} />, bg: 'var(--operator-soft)', color: '#c2358f', title: '转人工坐席', text: '帮我转人工' },
]

function loadSessions() {
  try {
    return JSON.parse(localStorage.getItem('cs_sessions') || '[]')
  } catch {
    return []
  }
}

function saveSessions(list) {
  localStorage.setItem('cs_sessions', JSON.stringify(list.slice(0, 30)))
}

// 消息行：memo 避免长列表时整列重渲染
const MsgRow = memo(function MsgRow({ m, index, last, escalated, loading, onFeedback, feedback, onDelete, onCopy }) {
  if (m.role === 'system') {
    return (
      <div className="msg-row system">
        <div className="system-line">{m.content}</div>
      </div>
    )
  }
  return (
    <div className={`msg-row ${m.role} ${m.deleted ? 'deleted' : ''}`}>
      <Avatar kind={m.role} size={30} />
      <div className="msg-body">
        {m.image && (
          <img
            src={m.image.startsWith('data:') ? m.image : 'data:image/png;base64,' + m.image}
            alt="用户上传图"
            className="user-img"
          />
        )}
        <div className="bubble">{m.deleted ? '（已删除）' : m.content}</div>
        {m.role === 'agent' && !m.deleted && <MsgMeta msg={m} />}
        {m.ts && <span className="msg-time">{formatTime(m.ts)}</span>}
        {/* 消息操作：复制 / 删除。删除只影响本地视图，不请求后端（与主流对话应用一致） */}
        {!m.deleted && !m.streaming && (
          <div className="msg-actions">
            <button className="msg-action-btn" onClick={() => onCopy(m.content)} title="复制内容">
              <Copy size={11} /> 复制
            </button>
            <button className="msg-action-btn danger" onClick={() => onDelete(index)} title="从对话中删除这条">
              <Trash2 size={11} /> 删除
            </button>
          </div>
        )}
        {m.role === 'agent' && !m.deleted && (
          <FeedbackRow
            onFeedback={onFeedback}
            feedback={feedback}
            show={last && !escalated && !loading}
          />
        )}
      </div>
    </div>
  )
})

export default function VisitorChat() {
  const [sessions, setSessions] = useState(loadSessions)
  const [sessionId, setSessionId] = useState('')
  const [messages, setMessages] = useState([])
  const [image, setImage] = useState('')
  const [loading, setLoading] = useState(false)
  const [escalated, setEscalated] = useState(false)
  const [ticketId, setTicketId] = useState('')
  const [operatorName, setOperatorName] = useState('')
  const [error, setError] = useState('')
  const [memoryNote, setMemoryNote] = useState('')
  const [lastMsg, setLastMsg] = useState(null)
  const [feedback, setFeedback] = useState('')
  const [injectMsg, setInjectMsg] = useState('')
  const [webSearch, setWebSearch] = useState(false)
  const bodyRef = useRef(null)
  const visitorId = useRef(null)
  const { mobile } = useShell()

  // 草稿走 zustand（切页不丢输入）
  const draft = useAppStore((s) => s.draft['visitor'] ?? '')
  const setDraft = useAppStore((s) => s.setDraft)
  const [input, setInput] = useState(draft)
  useEffect(() => {
    // 挂载时回填一次；之后由 setDraft 同步
    if (draft) setInput(draft)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  const onInputChange = (v) => {
    setInput(v)
    setDraft('visitor', v)
  }
  const onPickImage = (e) => {
    const f = e.target.files?.[0]
    if (!f) return
    const reader = new FileReader()
    reader.onload = () => setImage(reader.result)
    reader.readAsDataURL(f)
  }

  // 键盘弹起时把输入区保持在可视范围内（visualViewport）
  const footRef = useRef(null)
  useEffect(() => {
    const vv = window.visualViewport
    if (!vv) return
    const onResize = () => {
      if (footRef.current) {
        footRef.current.style.bottom = Math.max(0, vv.offsetTop + vv.height - window.innerHeight) + 'px'
      }
    }
    vv.addEventListener('resize', onResize)
    vv.addEventListener('scroll', onResize)
    return () => {
      vv.removeEventListener('resize', onResize)
      vv.removeEventListener('scroll', onResize)
    }
  }, [])

  const ensureSession = useCallback(
    async (firstContent) => {
      if (sessionId) return sessionId
      if (!visitorId.current) visitorId.current = ensureVisitorId()
      const data = await http.post('/api/sessions', { visitor_id: visitorId.current })
      setSessionId(data.session_id)
      // 保存会话归属凭据，后续该会话所有请求自动带 X-Session-Secret
      if (data.session_secret) {
        useAppStore.getState().setSessionSecret(data.session_id, data.session_secret)
      }
      setSessions((prev) => {
        const next = [
          {
            sid: data.session_id,
            title: firstContent ? firstContent.slice(0, 20) : '新对话',
            ts: Date.now(),
          },
          ...prev.filter((s) => s.sid !== data.session_id),
        ]
        saveSessions(next)
        return next
      })
      return data.session_id
    },
    [sessionId],
  )

  // 老访客问候已下线：/api/memory 现在仅管理员可读（PII 保护），不再由访客端拉取。

  // WS 实时通道（useWs 自动重连；sessionId 为空时不连接）
  // 访客 WS 必须带 secret query，否则后端 close(4401)
  const wsPath = sessionId
    ? `/ws/${sessionId}?secret=${encodeURIComponent(useAppStore.getState().getSessionSecret(sessionId) || '')}`
    : null
  const { state: wsState } = useWs(
    wsPath,
    (p) => {
      if (p.type === 'operator_message') {
        setMessages((prev) => [
          ...prev,
          { role: 'operator', content: p.content, refs: [], ts: new Date().toISOString() },
        ])
        setLastMsg(null)
      } else if (p.type === 'agent_handled') {
        setOperatorName(p.operator || '坐席')
        setMessages((prev) => [
          ...prev,
          { role: 'system', content: `坐席 ${p.operator || '已接管'} 为您服务`, ts: new Date().toISOString() },
        ])
      } else if (p.type === 'session_closed') {
        setMessages((prev) => [
          ...prev,
          { role: 'system', content: '会话已关闭，感谢您的咨询。', ts: new Date().toISOString() },
        ])
      }
    },
  )

  useEffect(() => {
    if (bodyRef.current) bodyRef.current.scrollTop = bodyRef.current.scrollHeight
  }, [messages, loading])

  const openHistory = async (sid, title) => {
    setSessionId(sid)
    setEscalated(false)
    setOperatorName('')
    setLastMsg(null)
    setFeedback('')
    setInjectMsg('')
    try {
      const data = await http.get(`/api/sessions/${sid}/messages`)
      setMessages(
        (data.messages || []).map((m) => ({
          role: m.role === 'agent' ? 'agent' : m.role === 'operator' ? 'operator' : m.role === 'system' ? 'system' : 'visitor',
          content: m.content,
          refs: m.refs || [],
          ts: m.created_at,
          message_id: m.id,
        })),
      )
    } catch {
      setMessages([])
    }
    setSessions((prev) => {
      const next = prev.map((s) => (s.sid === sid ? { ...s, title: title || s.title } : s))
      saveSessions(next)
      return next
    })
  }

  const newChat = async () => {
    if (loading) return
    setSessionId('')
    setMessages([])
    setEscalated(false)
    setOperatorName('')
    setLastMsg(null)
    setFeedback('')
    setInjectMsg('')
    setError('')
    setMemoryNote('')
    setInput('')
    setDraft('visitor', '')
  }

  const send = async (text) => {
    const content = (text ?? input).trim()
    if (!content && !image) return
    if (loading) return
    setError('')
    setLastMsg(null)
    setFeedback('')
    const img = image.startsWith('data:') ? image.split(',')[1] : image
    const sid = await ensureSession(content)
    onInputChange('')
    setImage('')
    setMessages((prev) => [
      ...prev,
      { role: 'visitor', content: content || '（附图）', refs: [], ts: new Date().toISOString(), image: img || undefined },
    ])
    // 流式气泡占位（delta 增量填充，done 事件权威替换）
    setMessages((prev) => [...prev, { role: 'agent', content: '', refs: [], ts: new Date().toISOString(), streaming: true }])
    let streamBuf = ''
    const finalize = (data) => {
      const msg = {
        role: 'agent',
        content: data.text,
        refs: data.citations || [],
        ts: new Date().toISOString(),
        tool_used: data.tool_used,
        tool_result: data.tool_result,
        ctx_used: data.ctx_used,
        message_id: data.message_id,
      }
      setMessages((prev) => [...prev.slice(0, -1), msg])
      setLastMsg(msg)
      if (data.escalate) {
        setEscalated(true)
        setTicketId(data.ticket_id || '')
      }
      if (data.memory_note) setMemoryNote(data.memory_note)
      if (data.tool_used === 'injection_guard') {
        setInjectMsg('检测到越权/提示词注入输入，已拦截并记录。请就具体问题咨询。')
      }
    }
    setLoading(true)
    try {
      const sec = useAppStore.getState().getSessionSecret(sid)
      const resp = await fetch(`/api/sessions/${sid}/chat/stream`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(sec ? { 'X-Session-Secret': sec } : {}),
        },
        body: JSON.stringify({ content, image: img || null, web_search: webSearch }),
      })
      if (resp.ok && resp.body) {
        const reader = resp.body.getReader()
        const decoder = new TextDecoder()
        let buf = ''
        let done = false
        for (;;) {
          const { value, done: rdDone } = await reader.read()
          if (rdDone) break
          buf += decoder.decode(value, { stream: true })
          const parts = buf.split('\n\n')
          buf = parts.pop() || ''
          for (const part of parts) {
            const evLine = part.split('\n').find((l) => l.startsWith('event: '))
            const dataLine = part.split('\n').find((l) => l.startsWith('data: '))
            if (!evLine || !dataLine) continue
            const ev = evLine.slice(7)
            let payload
            try { payload = JSON.parse(dataLine.slice(6)) } catch { continue }
            if (ev === 'delta') {
              streamBuf += payload.text
              setMessages((prev) => [...prev.slice(0, -1), { role: 'agent', content: streamBuf, refs: [], ts: new Date().toISOString(), streaming: true }])
            } else if (ev === 'done') {
              done = true
              finalize(payload)
            }
          }
        }
        if (!done) {
          if (streamBuf) {
            // 流中断但有部分内容：以已有内容收尾，避免重复请求造成双条消息
            finalize({ text: streamBuf, citations: [] })
          } else {
            throw new Error('stream unavailable')
          }
        }
      } else {
        throw new Error('stream unavailable')
      }
    } catch (e) {
      // 回退：整段接口（流式不可用时依旧可用）
      try {
        const data = await http.post(`/api/sessions/${sid}/messages`, { content, image: img || null, web_search: webSearch })
        finalize(data)
      } catch (e2) {
        if (!streamBuf) setMessages((prev) => prev.slice(0, -1))
        setError(e2.message || '网络异常，请检查连接后重试')
      }
    } finally {
      setLoading(false)
    }
  }

  const giveFeedback = async (rating) => {
    if (!sessionId || !lastMsg) return
    setFeedback(rating)
    try {
      await http.post(`/api/sessions/${sessionId}/feedback`, { rating, message_id: lastMsg.message_id })
      if (rating === 'not_helpful') {
        setEscalated(true)
        setMessages((prev) => [
          ...prev,
          { role: 'system', content: '已为您转接人工坐席跟进。', ts: new Date().toISOString() },
        ])
      }
    } catch {}
  }

  const deleteMessage = useCallback((idx) => {
    setMessages((prev) => prev.map((m, i) => (i === idx ? { ...m, deleted: true } : m)))
    setLastMsg(null)
  }, [])

  const copyMessage = useCallback((text) => {
    if (!text) return
    navigator.clipboard?.writeText(text).catch(() => {})
  }, [])

  const hasMessages = messages.length > 0
  const isMobile = mobile

  // 移动端：左侧会话栏收起，新对话入口移到顶部
  return (
    <div className={`chat-app ${isMobile ? 'chat-app-mobile' : ''}`}>
      {isMobile && (
        <div className="mobile-side-trigger">
          <button className="btn ghost sm" onClick={newChat} disabled={loading}>
            <Plus size={14} /> 新对话
          </button>
        </div>
      )}
      {/* 左侧：会话栏（移动端隐藏） */}
      <aside className={`side ${isMobile ? 'side-mobile' : ''}`}>
        <div className="side-head">
          <button className="new-chat" onClick={newChat} disabled={loading}>
            <Plus size={16} />
            新对话
          </button>
        </div>
        <div className="side-section">
          <div className="side-label">快速场景</div>
          <div className="scene-grid">
            {SCENES.map((s, i) => (
              <button key={i} className="scene" onClick={() => send(s.text)} disabled={loading}>
                <span className="s-ico" style={{ background: s.bg, color: s.color }}>
                  {s.icon}
                </span>
                <span>
                  <span className="s-t">{s.title}</span>
                  <span className="s-d" style={{ display: 'block' }}>{s.text.slice(0, 18)}…</span>
                </span>
              </button>
            ))}
          </div>
        </div>
        {sessions.length > 0 && (
          <div className="side-section" style={{ flex: '0 1 auto' }}>
            <div className="side-label">历史对话</div>
            <div className="slist">
              {sessions.map((s) => (
                <div
                  key={s.sid}
                  className={`sitem ${s.sid === sessionId ? 'active' : ''}`}
                  onClick={() => openHistory(s.sid, s.title)}
                >
                  <span className="s-title">{s.title}</span>
                  <span className="s-meta">
                    {new Date(s.ts).toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric' })} · #{s.sid.slice(0, 6)}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}
        <div className="side-foot">
          <div className="agent-status">
            <span className={`dot ${wsState === 'online' ? 'online' : wsState === 'connecting' ? 'connecting' : 'offline'}`} />
            {/* 未开始对话时不显示连接状态：那时还没有 WS，显示"已断开"会误导用户 */}
            <span>
              {!sessionId
                ? '开始对话后建立实时通道'
                : wsState === 'online'
                  ? '实时通道在线'
                  : wsState === 'connecting'
                    ? '连接中…'
                    : '已断开，重连中'}
            </span>
          </div>
        </div>
      </aside>

      {/* 右侧：对话区 */}
      <main className={`stage ${isMobile ? 'stage-mobile' : ''}`}>
        <div className="stage-head">
          <div className="sh-left">
            <Avatar kind="agent" size={36} />
            <div>
              <div className="sh-title">知客在线客服</div>
              <div className="sh-sub">
                {sessionId ? `会话 #${sessionId.slice(0, 8)}` : '未开始对话'}
              </div>
            </div>
          </div>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            {escalated && (
              <span className="badge warn">
                <ShieldCheck size={12} />
                已转人工{operatorName ? ` · ${operatorName}` : ''}{ticketId}
              </span>
            )}
            <button className="btn ghost sm" onClick={newChat} title="重新开始">
              <RefreshCw size={14} />
            </button>
          </div>
        </div>

        {memoryNote && <div className="memory-banner"><User size={13} /> {memoryNote}</div>}
        {error && <div className="hint err" style={{ margin: '10px 24px 0' }}>{error}</div>}
        {injectMsg && <InjectBanner text={injectMsg} onClose={() => setInjectMsg('')} />}

        <div className="stage-body" ref={bodyRef}>
          {!hasMessages && !loading && (
            <div className="welcome">
              <div className="welcome-logo">
                <Logo size={56} />
              </div>
              <h2>您好，我是知客，你的在线客服 Agent</h2>
              <p>
                支持订单查询、退款申请、政策咨询，可上传截图（多模态），
                还能记住您的偏好。选一个场景开始，或直接输入问题。
              </p>
              <div className="welcome-cards">
                {SCENES.map((s, i) => (
                  <button key={i} className="wc" onClick={() => send(s.text)}>
                    <span className="wc-ico" style={{ background: s.bg, color: s.color }}>
                      {s.icon}
                    </span>
                    <span>
                      <span className="wc-t">{s.title}</span>
                      <span className="wc-d" style={{ display: 'block' }}>{s.text}</span>
                    </span>
                  </button>
                ))}
              </div>
            </div>
          )}

          {messages.map((m, i) => (
            <MsgRow
              key={i}
              m={m}
              index={i}
              last={i === messages.length - 1}
              escalated={escalated}
              loading={loading}
              onFeedback={giveFeedback}
              feedback={feedback}
              onDelete={deleteMessage}
              onCopy={copyMessage}
            />
          ))}

          {loading && (
            <div className="msg-row agent">
              <Avatar kind="agent" size={30} />
              <div className="bubble typing">
                <span className="t-dot" /> <span className="t-dot" /> <span className="t-dot" />
                <span className="t-label">Agent 正在处理…</span>
              </div>
            </div>
          )}

          {escalated && !operatorName && (
            <div className="escalate-bar">
              <Headset size={14} />
              已为您转接人工坐席，坐席在线后会实时出现在这里。
            </div>
          )}
        </div>

        <div className="stage-foot" ref={footRef} style={isMobile ? { position: 'sticky', bottom: 0 } : undefined}>
          <Composer
            value={input}
            onChange={onInputChange}
            onSend={() => send()}
            onPickImage={onPickImage}
            onRemoveImage={() => setImage('')}
            image={image}
            loading={loading}
            onToggleWebSearch={() => setWebSearch((v) => !v)}
            webSearchEnabled={webSearch}
          />
        </div>
      </main>
    </div>
  )
}

function formatTime(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
}
