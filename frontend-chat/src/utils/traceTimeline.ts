import type { TraceSpan } from './generation'

export interface TraceTimelineRow {
  span: TraceSpan
  depth: number
  stepNumber: string
  startMs: number
  endMs: number
  hasChildren: boolean
  children: TraceTimelineRow[]
  missingParent: boolean
  parallelWith: TraceSpan[]
}

export interface TraceTimeline {
  total: TraceSpan | null
  durationMs: number
  startsWithGeneration: boolean
  roots: TraceTimelineRow[]
  rows: TraceTimelineRow[]
}

export const traceStatusLabels: Record<TraceSpan['status'], string> = { ok: '完成', error: '失败', cancelled: '停止' }
export function traceSeconds(ms: number) { return (ms / 1000).toFixed(2) }
export function traceDuration(ms: number) { return ms > 0 && ms < 10 ? '< 0.01 秒' : `${traceSeconds(ms)} 秒` }

export function buildTraceTimeline(spans: TraceSpan[]): TraceTimeline {
  const byId = new Map(spans.map(span => [span.span_id, span]))
  if (byId.size !== spans.length) throw new Error('执行步骤编号重复，无法展示时间线')
  const totals = spans.filter(span => span.name === 'generation.execute')
  if (totals.length > 1 || (totals.length === 1 && totals[0].parent_span_id !== null)) {
    throw new Error('生成总耗时的记录关系无效')
  }
  const total = totals[0] ?? null
  if (!spans.length) return { total, durationMs: 0, startsWithGeneration: false, roots: [], rows: [] }
  const origin = Math.min(...spans.map(span => span.started_at_ms))
  const ordered = [...spans].sort((a, b) => a.started_at_ms - b.started_at_ms || a.span_id.localeCompare(b.span_id))
  const children = new Map<string | null, TraceTimelineRow[]>()
  const roots: TraceTimelineRow[] = []
  let durationMs = 0
  for (const span of ordered) {
    // 以存储时间的微秒精度计算边界，避免浮点时间相减使相接的步骤出现虚假重叠。
    const startUs = Math.round((span.started_at_ms - origin) * 1000)
    const endUs = startUs + Math.round(span.duration_ms * 1000)
    durationMs = Math.max(durationMs, endUs / 1000)
    if (span === total) continue
    const missingParent = span.parent_span_id !== null && !byId.has(span.parent_span_id)
    const row: TraceTimelineRow = { span, depth: 0, stepNumber: '', startMs: startUs / 1000, endMs: endUs / 1000,
      hasChildren: false, children: [], missingParent, parallelWith: [] }
    const siblings = children.get(span.parent_span_id)
    if (siblings) siblings.push(row)
    else children.set(span.parent_span_id, [row])
    if (span.parent_span_id === null || missingParent || span.parent_span_id === total?.span_id) roots.push(row)
  }
  // 只比较同一父步骤下的时间区间；父步骤包含子步骤不代表并行。
  for (const siblings of children.values()) {
    for (let i = 0; i < siblings.length; i++) {
      const current = siblings[i]
      if (current.endMs === current.startMs) continue
      for (const next of siblings.slice(i + 1)) {
        if (next.startMs >= current.endMs) break
        if (next.endMs === next.startMs) continue
        current.parallelWith.push(next.span)
        next.parallelWith.push(current.span)
      }
    }
  }
  const rows: TraceTimelineRow[] = []
  function append(row: TraceTimelineRow, depth: number, stepNumber: string) {
    const nested = children.get(row.span.span_id) ?? []
    row.depth = depth
    row.stepNumber = stepNumber
    row.children = nested
    row.hasChildren = nested.length > 0
    rows.push(row)
    nested.forEach((child, index) => append(child, depth + 1, `${stepNumber}.${index + 1}`))
  }
  roots.forEach((row, index) => append(row, 0, String(index + 1)))
  // 有环的节点无法从根访问，不能静默隐藏这些记录。
  if (rows.length !== spans.length - totals.length) throw new Error('执行步骤的父子关系存在循环')
  return { total, durationMs, startsWithGeneration: total !== null && total.started_at_ms === origin, roots, rows }
}
