/**
 * 角色卡管理（SillyTavern chara_card 对标）：
 * 卡片 CRUD、ST V2 JSON 导入、按群/人（chat_key）绑定——绑定后该会话人设与温度以卡为准。
 */
import { useCallback, useEffect, useState } from 'react'
import { Bot, Save, Trash2, Upload, Link2, Loader2 } from 'lucide-react'
import { http, isAuthError } from './lib/queries'
import { useFeedback } from './components/Feedback'
import { Spinner } from './components/Spinner'
import TokenGate from './components/TokenGate'
import { t } from './lib/t'

interface Card {
  id: number
  name: string
  persona: string
  greeting?: string | null
  temperature?: number | null
  tags?: string[]
}
interface Binding { chat_key: string; card_id: number | null; name?: string | null }

export default function CardsPanel() {
  const [cards, setCards] = useState<Card[]>([])
  const [bindings, setBindings] = useState<Binding[]>([])
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')
  const [authErr, setAuthErr] = useState(false)
  const [editing, setEditing] = useState<Partial<Card> | null>(null)
  const [stJson, setStJson] = useState('')
  const [bindKey, setBindKey] = useState('')
  const [bindCard, setBindCard] = useState('')
  const [busy, setBusy] = useState(false)
  const fb = useFeedback()

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const r = await http.get<{ cards: Card[]; bindings: Binding[] }>('/api/cards')
      setCards(r.cards || [])
      setBindings(r.bindings || [])
      setErr('')
    } catch (e) {
      if (isAuthError(e)) setAuthErr(true)
      else setErr((e as Error).message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  const save = async () => {
    if (!editing?.name?.trim() || !editing?.persona?.trim() || busy) return
    setBusy(true)
    try {
      await http.post('/api/cards', { card: editing })
      fb.toast.success('角色卡已保存')
      setEditing(null)
      await load()
    } catch (e) {
      fb.toast.error((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const importSt = async () => {
    if (!stJson.trim() || busy) return
    setBusy(true)
    try {
      const obj = JSON.parse(stJson)
      await http.post('/api/cards', { card: obj })
      fb.toast.success('SillyTavern 卡已导入')
      setStJson('')
      await load()
    } catch (e) {
      fb.toast.error(`导入失败：${(e as Error).message}`)
    } finally {
      setBusy(false)
    }
  }

  const remove = async (cid: number) => {
    try {
      await http.del(`/api/cards/${cid}`)
      fb.toast.success('已删除')
      await load()
    } catch (e) {
      fb.toast.error((e as Error).message)
    }
  }

  const bind = async () => {
    if (!bindKey.trim() || busy) return
    setBusy(true)
    try {
      await http.post('/api/cards/bind', {
        chat_key: bindKey.trim(),
        card_id: bindCard ? Number(bindCard) : null,
      })
      fb.toast.success(bindCard ? '已绑定' : '已解绑')
      setBindKey('')
      await load()
    } catch (e) {
      fb.toast.error((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  if (authErr) return <TokenGate onAuthed={() => load()} />
  if (loading) return <Spinner label={t('加载角色卡…')} />
  if (err) return <div className="hint err">{err}</div>

  return (
    <div className="cards-app">
      {/* 卡片列表 */}
      <section className="bp-sec">
        <div className="bp-sec-head">
          <h2><Bot size={15} className="bp-flask" /> {t('角色卡库')}</h2>
          <span>{t('绑定到群/人后，该会话的人设与温度以卡为准（chat_key 如 qq_group:12345 / qq:67890）')}</span>
        </div>
        {cards.length === 0 && <div className="bp-hint">{t('还没有角色卡——新建一张，或直接粘贴 SillyTavern V2 卡 JSON 导入。')}</div>}
        <div className="bp-roles">
          {cards.map((c) => {
            const bound = bindings.filter((b) => b.card_id === c.id).map((b) => b.chat_key)
            return (
              <div key={c.id} className="bp-role">
                <div className="bp-role-main" onClick={() => setEditing(c)} title={t('点击编辑')}>
                  <b>{c.name}</b>
                  <span>{(c.persona || '').slice(0, 60)}…</span>
                  <em>
                    {(c.tags || []).join(' · ')}
                    {bound.length > 0 && ` · 已绑定: ${bound.join(', ')}`}
                  </em>
                </div>
                <button className="btn secondary sm ac-del" title={t('删除')}
                  onClick={() => { if (confirm(`删除角色卡「${c.name}」？`)) remove(c.id) }}>
                  <Trash2 size={13} />
                </button>
              </div>
            )
          })}
        </div>
        <button className="btn secondary sm" style={{ marginTop: 10 }}
          onClick={() => setEditing({ name: '', persona: '', greeting: '', tags: [] })}>
          + 新建角色卡
        </button>
      </section>

      {/* 编辑器 */}
      {editing && (
        <section className="bp-sec">
          <div className="bp-sec-head"><h2>{editing.id ? '编辑卡' : '新建卡'}</h2><span>{editing.name || '未命名'}</span></div>
          <div className="ac-form">
            <input className="ac-input" placeholder={t('卡名（如：傲娇管家）')}
              value={editing.name || ''} onChange={(e) => setEditing({ ...editing, name: e.target.value })} />
            <textarea className="ac-input ac-answer" rows={8} placeholder={t('人设全文（覆盖默认人设；可含性格、口癖、称呼、禁区…）')}
              value={editing.persona || ''} onChange={(e) => setEditing({ ...editing, persona: e.target.value })} />
            <input className="ac-input" placeholder={t('开场白（可选，保留）')}
              value={editing.greeting || ''} onChange={(e) => setEditing({ ...editing, greeting: e.target.value })} />
            <input className="ac-input" placeholder={t('温度（可选，0~1，如 0.7）')} inputMode="decimal"
              value={editing.temperature ?? ''} onChange={(e) => {
                const v = e.target.value
                setEditing({ ...editing, temperature: v === '' ? null : Number(v) })
              }} />
            <div style={{ display: 'flex', gap: 8 }}>
              <button className="btn sm" onClick={save} disabled={busy}>
                {busy ? <Loader2 size={13} className="animate-spin" /> : <Save size={13} />} 保存
              </button>
              <button className="btn secondary sm" onClick={() => setEditing(null)}>{t('取消')}</button>
            </div>
          </div>
        </section>
      )}

      {/* ST V2 导入 */}
      <section className="bp-sec">
        <div className="bp-sec-head"><h2><Upload size={15} /> {t('导入 SillyTavern 卡')}</h2><span>{t('粘贴 chara_card_v2 JSON，自动转换')}</span></div>
        <textarea className="ac-input ac-answer" rows={4} placeholder='{"spec":"chara_card_v2","data":{"name":"...","description":"..."}}'
          value={stJson} onChange={(e) => setStJson(e.target.value)} />
        <button className="btn sm" style={{ marginTop: 8 }} onClick={importSt} disabled={!stJson.trim() || busy}>
          <Upload size={13} /> 导入
        </button>
      </section>

      {/* 绑定管理 */}
      <section className="bp-sec">
        <div className="bp-sec-head"><h2><Link2 size={15} /> {t('会话绑定')}</h2><span>{t('一个 chat_key 只绑一张卡；留空卡=解绑')}</span></div>
        <div className="bp-role-save">
          <input placeholder="chat_key（qq_group:12345 / qq:67890）"
            value={bindKey} onChange={(e) => setBindKey(e.target.value)} />
          <select className="ac-input" style={{ width: 220 }} value={bindCard} onChange={(e) => setBindCard(e.target.value)}>
            <option value="">{t('（解绑）')}</option>
            {cards.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
          <button className="btn sm" onClick={bind} disabled={!bindKey.trim() || busy}><Link2 size={13} /> {t('绑定')}</button>
        </div>
        {bindings.length > 0 && (
          <div className="bp-hint" style={{ marginTop: 8 }}>
            当前绑定：{bindings.map((b) => `${b.chat_key}→${b.name || '?'}`).join('，')}
          </div>
        )}
      </section>
    </div>
  )
}
