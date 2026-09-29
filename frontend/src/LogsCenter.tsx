import { useMemo, useState } from 'react'
import { ScrollText, RefreshCw } from 'lucide-react'
import { useAdminLogs } from './lib/queries'
import { Spinner, ErrorBanner } from './components/Spinner'
import { EmptyState, PageHead } from './ui'
import './pages2.css'

const LEVELS = [
  { key: '', label: '全部' },
  { key: 'INFO', label: 'INFO' },
  { key: 'WARNING', label: 'WARNING' },
  { key: 'ERROR', label: 'ERROR' },
]

function levelColor(level: string): string {
  if (level === 'ERROR') return 'text-danger'
  if (level === 'WARNING') return 'text-warn'
  if (level === 'DEBUG') return 'text-muted'
  return 'text-ink-2'
}

function fmtTime(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString('zh-CN', { hour12: false })
  } catch {
    return iso
  }
}

export default function LogsCenter() {
  const logs = useAdminLogs()
  const [level, setLevel] = useState('')

  const entries = useMemo(
    () => (logs.data?.entries ?? []).filter((e) => !level || e.level === level),
    [logs.data, level],
  )

  return (
    <div className="p-4 md:p-6 max-w-[1400px] mx-auto w-full space-y-4 pb-24 md:pb-10">
      <PageHead
        title="日志中心"
        sub="Agent 每一步的运行事件 · 3 秒自动刷新 · 保留最近 1000 条"
        right={
          <>
            {LEVELS.map((l) => (
              <button
                key={l.key}
                onClick={() => setLevel(l.key)}
                className={`mod-toggle ${level === l.key ? 'on' : ''}`}
              >
                {l.label}
              </button>
            ))}
            <button className="btn ghost sm" onClick={() => logs.refetch()} title="立即刷新">
              <RefreshCw size={13} />
            </button>
          </>
        }
      />

      {logs.isLoading && <Spinner full label="加载日志…" />}
      {logs.error && <ErrorBanner message={(logs.error as Error).message} onRetry={() => logs.refetch()} />}

      {entries.length === 0 && !logs.isLoading && (
        <EmptyState
          icon={<ScrollText size={22} />}
          title="暂无匹配日志"
          desc="访客发消息、护栏拦截、工具调用、转人工都会实时出现在这里。"
        />
      )}

      {entries.length > 0 && (
        <div className="bg-panel border border-line-2 rounded-xl p-3 font-mono text-xs leading-6 overflow-auto md:max-h-[calc(100dvh-200px)]">
          {entries.map((e) => (
            <div key={e.id} className="flex gap-3 border-b border-line-2 py-1 px-2 rounded hover:bg-panel-2">
              <span className="text-muted shrink-0">{fmtTime(e.time)}</span>
              <span className={`shrink-0 w-[64px] font-semibold ${levelColor(e.level)}`}>{e.level}</span>
              <span className="text-muted shrink-0 hidden md:inline w-[80px] truncate" title={e.name}>{e.name}</span>
              <span className="break-all">{e.message}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
