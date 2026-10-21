import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { qk } from './queryClient'
import { useAppStore } from '../store/appStore'
import type {
  AgentConfig, KbEntry, AdminOverview, AdminStats, MemoryEntry,
  InjectionLogEntry, ToolDef, InjectionRule, AgentHealth,
  SessionRow, LogEntry, ChannelStatus,
} from '../types/api'

function baseHeaders(url?: string): Record<string, string> {
  const h: Record<string, string> = { 'Content-Type': 'application/json' }
  const t = useAppStore.getState().adminToken
  if (t) h['X-Admin-Token'] = t
  // 自动从 /api/sessions/{sid}/... 路径提取 sid，带上对应 X-Session-Secret
  if (url) {
    const m = url.match(/\/api\/sessions\/([a-f0-9]+)\//)
    if (m) {
      const sec = useAppStore.getState().getSessionSecret(m[1])
      if (sec) h['X-Session-Secret'] = sec
    }
  }
  return h
}

/** 带 HTTP 状态码的错误：401/503 用来区分「未登录/管理接口被禁用」，而不是当成普通失败。 */
export class HttpError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
    this.name = 'HttpError'
  }
}

export function isAuthError(e: unknown): boolean {
  return e instanceof HttpError && (e.status === 401 || e.status === 403 || e.status === 503)
}

async function fail(r: Response): Promise<never> {
  const detail = await r.json().catch(() => ({} as { detail?: string }))
  throw new HttpError(r.status, (detail as { detail?: string }).detail || `请求失败（HTTP ${r.status}）`)
}

const http = {
  get: async <T>(url: string): Promise<T> => {
    const r = await fetch(url, { headers: baseHeaders(url) })
    // 必须校验状态码：此前直接 .json()，401 的 {"detail":...} 被当成配置数据，
    // 导致「需要令牌」分支永远触发不了、界面白屏空转。
    if (!r.ok) await fail(r)
    return r.json() as Promise<T>
  },
  post: async <T>(url: string, body?: unknown): Promise<T> => {
    const r = await fetch(url, { method: 'POST', headers: baseHeaders(url), body: body === undefined ? undefined : JSON.stringify(body) })
    if (!r.ok) await fail(r)
    return r.json() as Promise<T>
  },
  put: async <T>(url: string, body?: unknown): Promise<T> => {
    const r = await fetch(url, { method: 'PUT', headers: baseHeaders(url), body: body === undefined ? undefined : JSON.stringify(body) })
    if (!r.ok) await fail(r)
    return r.json() as Promise<T>
  },
  del: async <T>(url: string): Promise<T> => {
    const r = await fetch(url, { method: 'DELETE', headers: baseHeaders(url) })
    if (!r.ok) await fail(r)
    return r.json() as Promise<T>
  },
  postForm: async <T>(url: string, form: FormData): Promise<T> => {
    const t = useAppStore.getState().adminToken
    const r = await fetch(url, { method: 'POST', headers: t ? { 'X-Admin-Token': t } : {}, body: form })
    if (!r.ok) await fail(r)
    return r.json() as Promise<T>
  },
}

// ---- config ----
export function useConfig() {
  return useQuery({ queryKey: qk.config, queryFn: () => http.get<{ config: AgentConfig }>('/api/config') })
}
export function useUpdateConfig() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (patch: Partial<AgentConfig>) => http.put<{ config: AgentConfig }>('/api/config', { config: patch }),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.config }),
  })
}

// ---- kb ----
export function useKb() {
  return useQuery({ queryKey: qk.kb, queryFn: () => http.get<{ kb: KbEntry[]; embedding?: { available: boolean; enabled: string; model: string } }>('/api/kb') })
}
export function useKbMutations() {
  const qc = useQueryClient()
  const invalidate = () => qc.invalidateQueries({ queryKey: qk.kb })
  return {
    create: useMutation({ mutationFn: (e: KbEntry) => http.post<{ kb: KbEntry[] }>('/api/kb', e), onSuccess: invalidate }),
    update: useMutation({ mutationFn: (e: KbEntry) => http.put<{ kb: KbEntry[] }>(`/api/kb/${e.id}`, e), onSuccess: invalidate }),
    remove: useMutation({ mutationFn: (id: string) => http.del<{ kb: KbEntry[] }>(`/api/kb/${id}`), onSuccess: invalidate }),
  }
}

// ---- kb 批量导入/导出 ----
export function useKbImport() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (p: { text: string; replace: boolean }) =>
      http.post<{ added: number; total: number }>('/api/kb/import', p),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.kb }),
  })
}

export function useKbTest() {
  return useMutation({
    mutationFn: (query: string) =>
      http.post<{ query: string; top_k: number; min_overlap: number; embedding: string; hits: { id: string; title: string; score: number; answer: string }[] }>('/api/kb/test', { query }),
  })
}

export function useKbDocImport() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (file: File) => {
      const fd = new FormData()
      fd.append('file', file)
      return http.postForm<{ added: number; total: number; file: string }>('/api/kb/import/document', fd)
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.kb }),
  })
}

export async function exportKb(): Promise<void> {
  const r = await fetch('/api/kb/export', { headers: baseHeaders() })
  if (!r.ok) throw new Error('导出失败')
  const blob = await r.blob()
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = 'zhike-kb.md'
  a.click()
  URL.revokeObjectURL(a.href)
}

// ---- tools ----
export function useTools() {
  return useQuery({ queryKey: qk.tools, queryFn: () => http.get<{ tools: ToolDef[] }>('/api/tools') })
}
export function useToolMutations() {
  const qc = useQueryClient()
  const invalidate = () => qc.invalidateQueries({ queryKey: qk.tools })
  return {
    create: useMutation({ mutationFn: (t: ToolDef) => http.post<{ tool: ToolDef }>('/api/tools', t), onSuccess: invalidate }),
    update: useMutation({
      mutationFn: ({ id, ...t }: ToolDef & { id: string }) => http.put<{ tool: ToolDef }>(`/api/tools/${id}`, t),
      onSuccess: invalidate,
    }),
    toggle: useMutation({
      mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
        http.put<{ tool: ToolDef }>(`/api/tools/${id}`, { id, name: '', desc: '', args_json: {}, enabled }),
      onSuccess: invalidate,
    }),
    remove: useMutation({ mutationFn: (id: string) => http.del(`/api/tools/${id}`), onSuccess: invalidate }),
  }
}

// ---- injection rules ----
export function useInjectionRules() {
  return useQuery({ queryKey: qk.injectionRules, queryFn: () => http.get<{ rules: InjectionRule[] }>('/api/injection-rules') })
}
export function useInjectionRuleMutations() {
  const qc = useQueryClient()
  const invalidate = () => qc.invalidateQueries({ queryKey: qk.injectionRules })
  return {
    create: useMutation({ mutationFn: (r: { expr: string; action: 'block' | 'log_only'; enabled: boolean }) => http.post<{ rule: InjectionRule }>('/api/injection-rules', r), onSuccess: invalidate }),
    update: useMutation({ mutationFn: (r: InjectionRule) => http.put<{ rule: InjectionRule }>(`/api/injection-rules/${r.id}`, r), onSuccess: invalidate }),
    remove: useMutation({ mutationFn: (id: number) => http.del(`/api/injection-rules/${id}`), onSuccess: invalidate }),
  }
}

// ---- admin ----
export function useAdminOverview() {
  return useQuery({ queryKey: qk.admin.overview, queryFn: () => http.get<AdminOverview>('/api/admin/overview') })
}
export function useAdminStats() {
  return useQuery({
    queryKey: qk.admin.stats,
    queryFn: () => http.get<AdminStats>('/api/admin/stats'),
    refetchInterval: 30_000,
  })
}
export function useAdminMemory() {
  return useQuery({ queryKey: qk.admin.memory, queryFn: () => http.get<{ entries: MemoryEntry[] }>('/api/admin/memory') })
}
export function useAdminLogs() {
  return useQuery({
    queryKey: qk.admin.logs,
    queryFn: () => http.get<{ entries: LogEntry[] }>('/api/admin/logs?limit=300'),
    refetchInterval: 3000,
  })
}
export function useAdminChannels() {
  return useQuery({
    queryKey: qk.admin.channels,
    queryFn: () => http.get<{ channels: ChannelStatus[] }>('/api/admin/channels'),
    refetchInterval: 15_000,
  })
}
export function useAdminSessions(status?: string, q?: string) {
  const params = new URLSearchParams()
  if (status) params.set('status', status)
  if (q) params.set('q', q)
  const qs = params.toString() ? `?${params.toString()}` : ''
  return useQuery({
    queryKey: ['admin', 'sessions', status || 'all', q || ''],
    queryFn: () => http.get<{ total: number; items: SessionRow[] }>(`/api/admin/sessions${qs}`),
    refetchInterval: 10_000,
  })
}
export function useCloseSession() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (sid: string) => http.post<{ session_id: string; status: string }>(`/api/operator/close/${sid}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['admin', 'sessions'] })
      qc.invalidateQueries({ queryKey: qk.admin.overview })
    },
  })
}
export function useLlmTest() {
  return useMutation({
    mutationFn: () => http.post<{ configured: boolean; ok: boolean; error?: string | null; latency_ms?: number; reply?: string; base_url?: string; model?: string; source?: string }>('/api/admin/llm-test'),
  })
}
export function useLlmModels() {
  return useMutation({
    mutationFn: () => http.get<{ configured: boolean; ok: boolean; models: string[]; count?: number; error?: string | null; base_url?: string; source?: string }>('/api/admin/llm-models'),
  })
}
export function useAdminReport() {
  return useQuery({
    queryKey: ['admin', 'report'],
    queryFn: () => http.get<{
      days: number
      daily: { day: string; sessions: number; agent_replies: number; kb_hits: number; escalations: number }[]
      totals: { sessions: number; agent_replies: number; kb_hits: number; escalations: number; kb_hit_rate: number | null; escalate_rate: number | null; csat: number | null; csat_n: number }
    }>('/api/admin/report?days=7'),
    refetchInterval: 60_000,
  })
}

export function useAdminInjection() {
  return useQuery({ queryKey: qk.admin.injection, queryFn: () => http.get<{ entries: InjectionLogEntry[] }>('/api/admin/injection-log') })
}

// ---- 动作层 ----
export interface ActionLogEntry {
  id: string
  action: string
  risk: number
  params: Record<string, unknown>
  principal: string
  effective_tier: number | null
  session_id: string | null
  status: string
  approval_id: string | null
  result?: unknown
  error?: string | null
  created_at: string
  executed_at?: string | null
  duration_ms?: number | null
}

export interface ActionCatalog {
  actions: { name: string; summary: string; risk: number; risk_label: string; group: string; requires_approval: boolean }[]
  risks: { L0: number; L1: number; L2: number }
  agent_tier: number
  tier_label: string
  l2_enabled: boolean
}

export interface ApprovalEntry {
  id: string
  action_log_id: string
  action: string
  summary: string
  status: string
  expires_at: string
  created_at: string
}

export function useActionCatalog() {
  return useQuery({ queryKey: ['actions', 'catalog'], queryFn: () => http.get<ActionCatalog>('/api/actions/catalog') })
}

export function useActionLogs(params: { status?: string; action?: string; risk?: number } = {}) {
  const qs = new URLSearchParams()
  if (params.status) qs.set('status', params.status)
  if (params.action) qs.set('action', params.action)
  if (params.risk !== undefined) qs.set('risk', String(params.risk))
  qs.set('limit', '200')
  return useQuery({
    queryKey: ['actions', 'logs', params.status || '', params.action || '', params.risk ?? ''],
    queryFn: () => http.get<{ entries: ActionLogEntry[] }>(`/api/actions/logs?${qs.toString()}`),
    refetchInterval: 15_000,
  })
}

export function useApprovals(status = 'pending') {
  return useQuery({
    queryKey: ['approvals', status],
    queryFn: () => http.get<{ entries: ApprovalEntry[] }>(`/api/approvals?status=${status}`),
    refetchInterval: 10_000,
  })
}

export function useApprovalMutations() {
  const qc = useQueryClient()
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['approvals'] })
    qc.invalidateQueries({ queryKey: ['actions'] })
  }
  return {
    approve: useMutation({
      mutationFn: (id: string) => http.post<{ status: string }>(`/api/approvals/${id}/approve`),
      onSuccess: invalidate,
    }),
    deny: useMutation({
      mutationFn: ({ id, reason }: { id: string; reason?: string }) =>
        http.post<{ status: string }>(`/api/approvals/${id}/deny`, { reason: reason || '' }),
      onSuccess: invalidate,
    }),
  }
}

export function useAuditVerify() {
  return useMutation({
    mutationFn: () => http.get<{ ok: boolean; checked: number; broken_at: number | null }>('/api/admin/audit/verify'),
  })
}

export function useExecutorHealth() {
  return useQuery({
    queryKey: ['actions', 'executor'],
    queryFn: () => http.get<{ online: boolean; connected?: boolean; self_id?: string; error?: string }>('/api/executor/health'),
    refetchInterval: 20_000,
  })
}


// ---- health ----
export function useAgentHealth() {
  return useQuery({
    queryKey: qk.health,
    queryFn: () => http.get<AgentHealth>('/api/agent/health'),
    refetchInterval: 30_000,
  })
}

// ---- operator queues（refetchInterval 替代 15s 手写轮询）----
export function useOperatorQueues() {
  return useQuery({
    queryKey: ['operator', 'queues'],
    queryFn: () => http.get<{ sessions: []; tickets: [] }>('/api/operator/queues'),
    refetchInterval: 15_000,
  })
}

export { http }

// ---- Agent 多步任务 ----
export interface AgentRunStep {
  id: number
  step_no: number
  thought: string
  action: string | null
  observation: string
  status: string
  duration_ms: number
  created_at: string
}

export interface AgentRun {
  id: string
  session_id: string | null
  principal: string
  goal: string
  status: string
  tier: number | null
  started_at: string
  ended_at?: string | null
}

export function useAgentRuns(status?: string) {
  const qs = status ? `?status=${encodeURIComponent(status)}` : ''
  return useQuery({
    queryKey: ['agent', 'runs', status || 'all'],
    queryFn: () => http.get<{ entries: AgentRun[] }>(`/api/agent/runs${qs}`),
    refetchInterval: 10_000,
  })
}

export function useAgentRunDetail(runId: string | null) {
  return useQuery({
    queryKey: ['agent', 'run', runId],
    queryFn: () => http.get<{ run: Record<string, unknown>; steps: AgentRunStep[] }>(`/api/agent/runs/${runId}`),
    enabled: !!runId,
  })
}

export function useStartAgentRun() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: { goal: string; principal?: string; max_steps?: number }) =>
      http.post<{ run_id: string; status: string; answer: string; usage: Record<string, number>; steps: unknown[] }>(
        '/api/agent/run', body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['agent'] }),
  })
}
