import { useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { Pencil, Trash2, Upload, Download, Plus, Search, BookOpen, X, Save, RefreshCw, FileText } from 'lucide-react'
import { useKb, useKbMutations, useKbImport, useKbTest, useKbDocImport, exportKb, isAuthError } from '../lib/queries'
import type { KbEntry } from '../types/api'
import { Spinner, ErrorBanner } from '../components/Spinner'
import TokenGate from '../components/TokenGate'
import { EmptyState } from '../ui'
import { useFeedback } from '../components/Feedback'
import { t as tr } from '../lib/t'
import { useAppStore } from '../store/appStore'

export function KbPanel() {
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

export function KbForm({ initial, onDone }: { initial: KbEntry | null; onDone: () => void }) {
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
