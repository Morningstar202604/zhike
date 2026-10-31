import { useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { ChevronLeft, ChevronRight, ChevronUp, ChevronDown } from 'lucide-react'
import { t as tr } from '../lib/t'

export function Grp({ id, title, note, children }: { id: string; title: string; note?: string; children: React.ReactNode }) {
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
export function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="f-row">
      <span className="f-lb">{label}</span>
      {children}
    </div>
  )
}


export function RepItem({ label, value, sub, tone = 'brand' }: { label: string; value: string | number; sub: string; tone?: string }) {
  const color = tone === 'ok' ? '#1a8f52' : tone === 'warn' ? '#b5731b' : tone === 'muted' ? 'var(--muted)' : 'var(--brand)'
  return (
    <div className="rep-item">
      <span className="rep-label">{label}</span>
      <span className="rep-value" style={{ color }}>{value}</span>
      <span className="rep-sub">{sub}</span>
    </div>
  )
}

export function Pager({ page, maxPage, total, onChange, label }: { page: number; maxPage: number; total: number; onChange: (p: number) => void; label: string }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 12, fontSize: 12, color: 'var(--muted)' }}>
      <span>{tr('分页状态', { total, page: page + 1, pages: maxPage + 1 })}</span>
      <button className="btn ghost sm" disabled={page === 0} onClick={() => onChange(page - 1)}><ChevronLeft size={14} /></button>
      <button className="btn ghost sm" disabled={page >= maxPage} onClick={() => onChange(page + 1)}><ChevronRight size={14} /></button>
    </div>
  )
}

/* ============ 长期记忆 ============ */
