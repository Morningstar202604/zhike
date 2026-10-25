/**
 * 提醒与待办：秘书的核心能力——「10 分钟后提醒我关火」「明天 9 点叫我」。
 *
 * 数据流：这里建的提醒由 core/reminders.py 的调度器每 30 秒扫一次，
 * 到点自动推回原渠道（QQ 群/私聊直接发消息，网页则写入会话）。
 */
import { useCallback, useEffect, useState } from 'react'
import { Bell, Check, Clock, Loader2, Plus, Trash2, RefreshCw } from 'lucide-react'
import { http, isAuthError } from './lib/queries'
import { useFeedback } from './components/Feedback'
import { Spinner } from './components/Spinner'
import TokenGate from './components/TokenGate'
import { EmptyState, PageHead } from './ui'
import './pages.css'
import { t } from './lib/t'

interface Reminder {
  id: number
  visitor_id: string
  chat_key: string
  channel_type: string
  text: string
  due_at: string
  status: string
  created_at: string
  repeat_rule?: string
  last_error?: string | null
}

const PRESETS = [
  { label: '10 分钟后', when: '10分钟后' },
  { label: '1 小时后', when: '1小时后' },
  { label: '明天 9 点', when: '明天9点' },
  { label: '3 天后', when: '3天后' },
]

/** 周期提醒的快捷写法：直接把规则写进 when，后端 _parse_repeat 会识别 */
const REPEAT_PRESETS = [
  { label: '每天 8 点', when: '每天8点' },
  { label: '每个工作日 9 点', when: '每个工作日9点' },
  { label: '每周一 10 点', when: '每周一10点' },
  { label: '每月 1 号 9 点', when: '每月1号9点' },
]

const REPEAT_LABEL: Record<string, string> = {
  daily: '每天',
  weekday: '工作日',
  weekly: '每周',
  monthly: '每月',
}

function repeatText(rule?: string): string {
  if (!rule) return ''
  const [kind] = rule.split(':')
  return t(REPEAT_LABEL[kind] || '')
}

function fmt(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getMonth() + 1}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

export default function Reminders() {
  const [rows, setRows] = useState<Reminder[]>([])
  const [loading, setLoading] = useState(true)
  const [authErr, setAuthErr] = useState(false)
  const [err, setErr] = useState('')
  const [text, setText] = useState('')
  const [when, setWhen] = useState('10分钟后')
  const [busy, setBusy] = useState(false)
  const fb = useFeedback()

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const r = await http.get<{ reminders: Reminder[] }>('/api/reminders?status=pending')
      setRows(r.reminders || [])
      setErr('')
    } catch (e) {
      if (isAuthError(e)) setAuthErr(true)
      else setErr((e as Error).message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  const create = async () => {
    if (!text.trim() || busy) return
    setBusy(true)
    try {
      await http.post('/api/reminders', { text: text.trim(), when })
      fb.toast.success('提醒已创建')
      setText('')
      await load()
    } catch (e) {
      fb.toast.error((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const cancel = async (id: number) => {
    try {
      await http.del(`/api/reminders/${id}`)
      fb.toast.success('已取消')
      await load()
    } catch (e) {
      fb.toast.error((e as Error).message)
    }
  }

  const tickNow = async () => {
    try {
      const r = await http.post<{ sent: number }>('/api/reminders/tick')
      fb.toast.success(`已推送 ${r.sent} 条到期提醒`)
      await load()
    } catch (e) {
      fb.toast.error((e as Error).message)
    }
  }

  if (authErr) return <TokenGate onAuthed={() => load()} />
  if (loading) return <Spinner full label={t('加载提醒…')} />

  return (
    <div className="bp-app">
      <PageHead
        title={t('提醒与待办')}
        sub={t('说一句「10 分钟后提醒我关火」「每天 8 点提醒我吃药」就行，到点我会主动叫你——群里、私聊、网页都算')}
        right={
          <button className="btn secondary sm" onClick={tickNow} title={t('立即检查一次到期提醒')}>
            <RefreshCw size={13} /> {t('立即检查')}
          </button>
        }
      />

      <section className="bp-sec">
        <div className="bp-sec-head">
          <h2><Plus size={14} /> {t('新建提醒')}</h2>
          <span>{t('时间支持「10分钟后 / 1小时后 / 明天9点 / 3天后」')}</span>
        </div>
        <div className="ac-form">
          <input
            className="ac-input"
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder={t('要提醒你什么？如：给客户回电话')}
            maxLength={200}
            onKeyDown={(e) => { if (e.key === 'Enter') create() }}
          />
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            {PRESETS.map((p) => (
              <button
                key={p.when}
                className={`btn sm ${when === p.when ? '' : 'secondary'}`}
                onClick={() => setWhen(p.when)}
              >
                {t(p.label)}
              </button>
            ))}
            <input
              className="ac-input"
              style={{ maxWidth: 180 }}
              value={when}
              onChange={(e) => setWhen(e.target.value)}
              placeholder={t('或自定义时间')}
            />
          </div>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
            <span className="text-[11px] text-muted">{t('重复：')}</span>
            {REPEAT_PRESETS.map((p) => (
              <button
                key={p.when}
                className={`btn sm ${when === p.when ? '' : 'secondary'}`}
                onClick={() => setWhen(p.when)}
              >
                {t(p.label)}
              </button>
            ))}
          </div>
          <button className="btn" onClick={create} disabled={busy || !text.trim()}>
            {busy ? <Loader2 size={14} className="animate-spin" /> : <Bell size={14} />} {t('设提醒')}
          </button>
        </div>
      </section>

      <section className="bp-sec">
        <div className="bp-sec-head">
          <h2><Clock size={14} /> {t('待提醒')}</h2>
          <span>{rows.length}</span>
        </div>
        {err && <div className="hint err">{err}</div>}
        {rows.length === 0 ? (
          <EmptyState
            icon={<Bell size={22} />}
            title={t('没有待提醒的事')}
            desc={t('在聊天里直接说「明天 9 点提醒我开会」或「每天 8 点提醒我吃药」，也可以在上面新建。')}
          />
        ) : (
          <div className="ac-list">
            {rows.map((r) => (
              <div key={r.id} className="ac-item">
                <div className="ac-item-main">
                  <div className="ac-q"><Bell size={13} /> {r.text}</div>
                  <div className="ac-meta">
                    <em>{fmt(r.due_at)}</em>
                    {repeatText(r.repeat_rule) && <em className="text-brand">🔁 {repeatText(r.repeat_rule)}</em>}
                    <em>{r.channel_type.startsWith('qq') ? 'QQ' : t('网页')}</em>
                    <em>{r.chat_key}</em>
                    {r.last_error && <em title={r.last_error}>{t('上次投递失败')}</em>}
                  </div>
                </div>
                <div className="ac-item-ops">
                  <button className="btn secondary sm" onClick={() => cancel(r.id)}>
                    <Trash2 size={13} /> {t('取消')}
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  )
}
