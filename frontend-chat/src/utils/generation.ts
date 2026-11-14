import { isRecord, isCount } from './validation'
import type { Message } from '../api'

export type ResultKind = 'answered' | 'clarification' | 'no_evidence' | 'answer_rejected' | 'dependency_failed'
export interface GenerationSnapshot {
  generation_id: number
  request_id: string
  status: 'running' | 'completed' | 'failed' | 'cancelled'
  assistant_content: string | null
  assistant_message_id: number
  user_message_id: number
  kb_only: boolean
  project_id: string | null
  project_name: string | null
  project_deleted: boolean
  elapsed_seconds: number
  phase: string
  error: string | null
  trace_id: string | null
  result_kind: ResultKind | null
  error_code: string | null
}

export function nullableText(value: unknown): value is string | null {
  return value === null || typeof value === 'string'
}
export function resultKind(value: unknown): value is ResultKind | null {
  return value === null || value === 'answered' || value === 'clarification' || value === 'no_evidence'
    || value === 'answer_rejected' || value === 'dependency_failed'
}
export function parseGeneration(value: unknown): GenerationSnapshot {
  if (!isRecord(value) || !isCount(value.generation_id) || value.generation_id === 0
      || typeof value.request_id !== 'string'
      || (value.status !== 'running' && value.status !== 'completed' && value.status !== 'failed' && value.status !== 'cancelled')
      || !nullableText(value.assistant_content) || !isCount(value.assistant_message_id) || !isCount(value.user_message_id)
      || typeof value.kb_only !== 'boolean' || !nullableText(value.project_id) || !nullableText(value.project_name)
      || typeof value.project_deleted !== 'boolean' || typeof value.elapsed_seconds !== 'number'
      || !Number.isFinite(value.elapsed_seconds) || value.elapsed_seconds < 0 || typeof value.phase !== 'string'
      || !nullableText(value.error) || !nullableText(value.trace_id) || !resultKind(value.result_kind)
      || !nullableText(value.error_code)) throw new Error('回答状态数据格式无效，请刷新后重试')
  return { generation_id: value.generation_id, request_id: value.request_id, status: value.status,
    assistant_content: value.assistant_content, assistant_message_id: value.assistant_message_id,
    user_message_id: value.user_message_id, kb_only: value.kb_only, project_id: value.project_id,
    project_name: value.project_name, project_deleted: value.project_deleted, elapsed_seconds: value.elapsed_seconds,
    phase: value.phase, error: value.error, trace_id: value.trace_id, result_kind: value.result_kind, error_code: value.error_code }
}

export function parseMessageHistory(value: unknown): {
  messages: Message[]; next_before: number | null; kbOnly: boolean | null; project_id: string | null
  project_name: string | null; project_deleted: boolean; active_generation: { request_id: string } | null
} {
  if (!isRecord(value) || !Array.isArray(value.messages)
      || (value.next_before !== null && !isCount(value.next_before))
      || (value.kbOnly !== null && typeof value.kbOnly !== 'boolean')
      || !nullableText(value.project_id) || !nullableText(value.project_name) || typeof value.project_deleted !== 'boolean') {
    throw new Error('历史消息格式无效')
  }
  let active: { request_id: string } | null = null
  if (value.active_generation !== null) {
    if (!isRecord(value.active_generation) || typeof value.active_generation.request_id !== 'string') throw new Error('进行中的回答格式无效')
    active = { request_id: value.active_generation.request_id }
  }
  const messages = value.messages.map((item: unknown): Message => {
    if (!isRecord(item) || !isCount(item.id) || item.id === 0 || (item.role !== 'user' && item.role !== 'assistant')
        || typeof item.content !== 'string' || typeof item.kbOnly !== 'boolean'
        || (item.status !== 'streaming' && item.status !== 'completed' && item.status !== 'error' && item.status !== 'cancelled')
        || (item.generation_id !== null && (!isCount(item.generation_id) || item.generation_id === 0))
        || !nullableText(item.trace_id) || !resultKind(item.result_kind) || !nullableText(item.error_code)) {
      throw new Error('历史消息格式无效')
    }
    return { id: item.id, role: item.role, content: item.content, kbOnly: item.kbOnly, status: item.status,
      generation_id: item.generation_id, trace_id: item.trace_id, result_kind: item.result_kind, error_code: item.error_code }
  })
  return { messages, next_before: value.next_before, kbOnly: value.kbOnly, project_id: value.project_id,
    project_name: value.project_name, project_deleted: value.project_deleted,
    active_generation: active }
}

export interface TraceSpan {
  span_id: string
  parent_span_id: string | null
  name: string
  started_at_ms: number
  duration_ms: number
  status: 'ok' | 'error' | 'cancelled'
}
export interface TraceDetail {
  trace_id: string | null
  status: string
  result_kind: ResultKind | null
  error_code: string | null
  truncated: boolean
  spans: TraceSpan[]
}
function parseTraceStart(value: unknown): number {
  if (typeof value !== 'string') throw new Error('执行步骤的开始时间无效')
  const parts = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,6}))?(Z|[+-]\d{2}:\d{2})$/.exec(value)
  if (!parts) throw new Error('执行步骤的开始时间无效')
  const localTime = Date.parse(`${parts[1]}Z`)
  const seconds = Date.parse(`${parts[1]}${parts[3]}`)
  if (!Number.isFinite(seconds) || !Number.isFinite(localTime)
      || new Date(localTime).toISOString().slice(0, 19) !== parts[1]) throw new Error('执行步骤的开始时间无效')
  // 后端时间精确到微秒；Date.parse 会丢失毫秒以下部分，可能把先后执行误判为重叠。
  return seconds + (parts[2] === undefined ? 0 : Number(`0.${parts[2]}`) * 1000)
}
export function parseTrace(value: unknown): TraceDetail {
  if (!isRecord(value) || !nullableText(value.trace_id) || typeof value.status !== 'string'
      || !resultKind(value.result_kind) || !nullableText(value.error_code) || typeof value.truncated !== 'boolean'
      || !Array.isArray(value.spans)) throw new Error('执行记录格式无效')
  const spans = value.spans.map((item: unknown): TraceSpan => {
    if (!isRecord(item) || typeof item.span_id !== 'string' || typeof item.name !== 'string'
        || !nullableText(item.parent_span_id)
        || typeof item.duration_ms !== 'number' || !Number.isFinite(item.duration_ms) || item.duration_ms < 0
        || (item.status !== 'ok' && item.status !== 'error' && item.status !== 'cancelled')) throw new Error('执行步骤格式无效')
    return { span_id: item.span_id, parent_span_id: item.parent_span_id, name: item.name,
      started_at_ms: parseTraceStart(item.started_at), duration_ms: item.duration_ms, status: item.status }
  })
  return { trace_id: value.trace_id, status: value.status, result_kind: value.result_kind,
    error_code: value.error_code, truncated: value.truncated, spans }
}

export interface AuditEvent {
  id: number
  operation_id: string
  action: string
  result: string
  created_at: string
  actor_type: 'user' | 'system' | 'anonymous'
}
export function parseAuditPage(value: unknown): { events: AuditEvent[]; next_before: number | null } {
  if (!isRecord(value) || !Array.isArray(value.events) || (value.next_before !== null && !isCount(value.next_before))) {
    throw new Error('操作记录格式无效')
  }
  const events = value.events.map((item: unknown): AuditEvent => {
    if (!isRecord(item) || !isCount(item.id) || typeof item.operation_id !== 'string'
        || typeof item.action !== 'string' || typeof item.result !== 'string' || typeof item.created_at !== 'string'
        || !Number.isFinite(Date.parse(item.created_at))
        || (item.actor_type !== 'user' && item.actor_type !== 'system' && item.actor_type !== 'anonymous')) {
      throw new Error('操作记录格式无效')
    }
    return { id: item.id, operation_id: item.operation_id, action: item.action, result: item.result,
      created_at: item.created_at, actor_type: item.actor_type }
  })
  return { events, next_before: value.next_before }
}
