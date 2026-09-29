export interface AgentConfig {
  llm: {
    base_url: string
    model: string
    temperature: number
    top_p: number
    max_tokens: number
    has_key: boolean
    api_key?: string
    key_source?: string
  }
  retrieval: { kb_top_k: number; min_overlap: number }
  context: {
    history_turns: number
    char_budget: number
    compress_threshold: number
    summarize: boolean
    relevant_only: boolean
    modules: Record<string, boolean>
    persona: string
  }
  memory: {
    enable: boolean
    retrieval_top_k: number
    auto_capture: boolean
    capture_keywords: string[]
    visit_greeting: boolean
  }
  tools: Record<string, boolean>
  multimodal: { enable: boolean }
  guardrail: { injection_block: boolean }
  engine: { mode: 'rule' | 'llm' | 'auto' }
  escalation: {
    confidence_threshold: number
    max_message_len: number
    react_steps: number
  }
}

export interface KbEntry {
  id: string
  text: string
  title: string
  answer: string
  tags: string[]
}

export interface AdminOverview {
  sessions: number
  messages: number
  tickets: number
  pending: number
  feedback: { total: number; helpful: number; not_helpful: number; help_rate: number }
  kb_count: number
  injection: number
}

export interface AdminStats {
  messages_by_hour: { bucket: string; visitor: number; agent: number; operator: number }[]
  sessions_by_day: { day: string; count: number }[]
  escalate_reasons: { reason: string; count: number }[]
  tool_usage: { tool: string; count: number }[]
  channels: { channel: string; count: number }[]
  feedback: { helpful: number; not_helpful: number }
  funnel: {
    messages: number
    kb_answered: number
    tool_calls: number
    guardrail_blocked: number
    escalations: number
  }
}

export interface SessionRow {
  id: string
  visitor_id?: string
  status: string
  escalated?: number
  escalate_reason?: string | null
  assignee?: string | null
  started_at?: string
  ended_at?: string | null
  message_count: number
  channel: string
}

export interface LogEntry {
  id: number
  time: string
  level: string
  name: string
  message: string
}

export interface ChannelStatus {
  channel: string
  status: string
  last_seen?: number | null
  detail?: Record<string, unknown>
}

export interface MemoryEntry {
  visitor_id: string
  key: string
  value: string
  updated_at: string
}

export interface InjectionLogEntry {
  id: number
  session_id: string
  visitor_id: string
  raw_input: string
  detected: string
  action: string
  created_at: string
}

export interface ToolDef {
  id: string
  name: string
  desc: string
  args_json: Record<string, string>
  enabled: boolean
  builtin: boolean
}

export interface InjectionRule {
  id: number
  expr: string
  action: 'block' | 'log_only'
  enabled: boolean
}

export interface AgentMsg {
  id?: number
  role: 'agent' | 'visitor' | 'operator' | 'system'
  content: string
  refs?: string[]
  tool_used?: string | null
  tool_result?: unknown
  ctx_used?: Record<string, unknown>
  ts?: string
  created_at?: string
  image?: string
}

export interface PendingSession {
  id: string
  visitor_id?: string
  escalate_reason?: string
  started_at?: string
}

export interface Ticket {
  id: string
  session_id: string
  priority: string
  status: string
  created_at: string
}

export interface AgentHealth {
  mode: string
  engine_mode: string
  llm_ready: boolean
  model: string
  base_url: string
  kb_entries: number
  tools: string[]
  tool_count: number
  injection_rules: number
  kb_top_k: number
  history_turns: number
  memory_enable: boolean
  escalate_threshold: number
  react_steps: number
}
