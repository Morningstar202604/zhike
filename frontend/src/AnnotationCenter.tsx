import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Plus, Trash2, Power, Loader2, Stamp, MessageSquareQuote } from 'lucide-react'
import { http, isAuthError } from './lib/queries'
import { useShell } from './components/ResponsiveShell'
import { Spinner } from './components/Spinner'
import TokenGate from './components/TokenGate'
import { PageHead, EmptyState } from './ui'

interface Annotation {
  id: number
  question: string
  answer: string
  enabled: number
  hits: number
  created_at: string
  updated_at: string
}

/** 标注回复（Dify annotations 对标）：
 *  常见问答标成标准答案，用户问到时零延迟直接回复，不走 KB/LLM。 */
export default function AnnotationCenter() {
  const { mobile } = useShell()
  const qc = useQueryClient()
  const [q, setQ] = useState('')
  const [a, setA] = useState('')
  const [busy, setBusy] = useState(false)
  const [editing, setEditing] = useState<number | null>(null)
  const [editDraft, setEditDraft] = useState({ question: '', answer: '' })

  const list = useQuery({
    queryKey: ['annotations'],
    queryFn: () => http.get<{ annotations: Annotation[] }>('/api/annotations'),
  })

  const invalidate = () => qc.invalidateQueries({ queryKey: ['annotations'] })

  const create = async () => {
    if (!q.trim() || !a.trim() || busy) return
    setBusy(true)
    try {
      await http.post('/api/annotations', { question: q.trim(), answer: a.trim() })
      setQ(''); setA('')
      invalidate()
    } catch (e) {
      alert((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const toggle = async (row: Annotation) => {
    await http.put(`/api/annotations/${row.id}`, { enabled: !row.enabled }).catch(() => {})
    invalidate()
  }

  const remove = async (row: Annotation) => {
    if (!confirm(`删除标注「${row.question.slice(0, 20)}」？`)) return
    await http.del(`/api/annotations/${row.id}`).catch(() => {})
    invalidate()
  }

  const saveEdit = async (id: number) => {
    await http.put(`/api/annotations/${id}`, editDraft).catch(() => {})
    setEditing(null)
    invalidate()
  }

  if (list.isLoading) return <Spinner full label="加载标注…" />
  if (list.error && isAuthError(list.error)) return <TokenGate error={list.error} onAuthed={() => list.refetch()} />

  const items = list.data?.annotations ?? []

  return (
    <div className={`bp-app ${mobile ? 'bp-mobile' : ''}`}>
      <PageHead
        title="标注回复"
        sub="常见问答标成标准答案，命中直接回复（零延迟、不走模型）· 对标 Dify Annotations"
      />

      <section className="bp-sec">
        <div className="bp-sec-head">
          <h2><Plus size={14} /> 新建标注</h2>
          <span>问法支持长短变体（归一化 + 包含匹配），命中自动计数</span>
        </div>
        <div className="ac-form">
          <input
            className="ac-input"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="用户会怎么问？如：退货政策是什么"
            maxLength={500}
          />
          <textarea
            className="ac-input ac-answer"
            value={a}
            onChange={(e) => setA(e.target.value)}
            placeholder="标准答案（将原文回复给用户）"
            rows={3}
            maxLength={4000}
          />
          <button className="btn" onClick={create} disabled={busy || !q.trim() || !a.trim()}>
            {busy ? <Loader2 size={14} className="animate-spin" /> : <Stamp size={14} />} 添加标注
          </button>
        </div>
      </section>

      <section className="bp-sec">
        <div className="bp-sec-head">
          <h2><MessageSquareQuote size={14} /> 标注列表</h2>
          <span>{items.length} 条</span>
        </div>
        {items.length === 0 ? (
          <EmptyState
            icon={<Stamp size={22} />}
            title="还没有标注"
            desc="把最常被问到的 3-5 个问题标成标准答案，客服机器人立刻变得又快又稳。"
          />
        ) : (
          <div className="ac-list">
            {items.map((row) => (
              <div key={row.id} className={`ac-item ${row.enabled ? '' : 'off'}`}>
                <div className="ac-item-main">
                  {editing === row.id ? (
                    <div style={{ display: 'grid', gap: 8 }}>
                      <input className="ac-input" value={editDraft.question}
                        onChange={(e) => setEditDraft({ ...editDraft, question: e.target.value })} />
                      <textarea className="ac-input" rows={3} value={editDraft.answer}
                        onChange={(e) => setEditDraft({ ...editDraft, answer: e.target.value })} />
                      <div style={{ display: 'flex', gap: 8 }}>
                        <button className="btn sm" onClick={() => saveEdit(row.id)}>保存</button>
                        <button className="btn secondary sm" onClick={() => setEditing(null)}>取消</button>
                      </div>
                    </div>
                  ) : (
                    <>
                      <div className="ac-q"><MessageSquareQuote size={13} /> {row.question}</div>
                      <div className="ac-a">{row.answer}</div>
                      <div className="ac-meta">
                        <em>命中 {row.hits} 次</em>
                        <em>{row.enabled ? '启用中' : '已停用'}</em>
                        <em>{(row.updated_at || '').slice(0, 10)}</em>
                      </div>
                    </>
                  )}
                </div>
                {editing !== row.id && (
                  <div className="ac-item-ops">
                    <button className="btn secondary sm" onClick={() => { setEditing(row.id); setEditDraft({ question: row.question, answer: row.answer }) }}>
                      编辑
                    </button>
                    <button className="btn secondary sm" onClick={() => toggle(row)} title={row.enabled ? '停用' : '启用'}>
                      <Power size={13} /> {row.enabled ? '停用' : '启用'}
                    </button>
                    <button className="btn secondary sm ac-del" onClick={() => remove(row)}>
                      <Trash2 size={13} />
                    </button>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  )
}
