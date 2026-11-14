<script setup lang="ts">
import { traceDuration, traceSeconds, traceStatusLabels, type TraceTimelineRow } from '../../utils/traceTimeline'

defineProps<{
  steps: TraceTimelineRow[]
  numberById: Record<string, string>
  nested?: boolean
}>()

const names: Record<string, string> = {
  'agent.execute': '问答流程', 'model.call': '模型调用',
  'subagent.retrieval': '知识库子任务', 'retrieval.report': '整理检索报告', 'retrieval.execute': '查询知识库',
  'lock.wait': '等待数据访问', 'retrieval.embedding': '问题向量化', 'retrieval.dense': '向量召回',
  'retrieval.bm25': '关键词召回', 'retrieval.fusion': '合并候选', 'retrieval.rerank': '相关性排序',
  'retrieval.evidence': '记录证据版本', 'answer.validate': '回答校验', 'answer.citations': '引用校验',
  'generation.save': '保存回答', 'provider.request': '外部服务请求', 'tool.tavily_search': '联网搜索',
}
</script>

<template>
  <ul class="execution-steps" :class="{ nested }">
    <li v-for="step in steps" :key="step.span.span_id">
      <component :is="step.hasChildren ? 'details' : 'div'" class="execution-step" :class="{ phase: step.hasChildren }" :open="step.hasChildren && step.depth === 0 ? true : undefined">
        <component :is="step.hasChildren ? 'summary' : 'div'" class="step-summary">
          <span class="step-number">{{ step.stepNumber }}</span>
          <span class="step-content">
            <span class="step-heading">
              <span class="step-name">{{ names[step.span.name] ?? step.span.name }}</span>
              <span v-if="step.hasChildren" class="phase-label">阶段 · {{ step.children.length }} 个子步骤</span>
              <span v-if="step.hasChildren" class="step-toggle" aria-hidden="true">▸</span>
            </span>
            <span class="step-time">开始 {{ traceSeconds(step.startMs) }} 秒 <span aria-hidden="true">→</span><span class="time-separator">，</span> 结束 {{ traceSeconds(step.endMs) }} 秒</span>
            <span v-if="step.parallelWith.length" class="step-parallel">与步骤 {{ step.parallelWith.map(span => numberById[span.span_id]).join('、') }} 并行</span>
            <span v-if="step.missingParent" class="step-missing">上级记录缺失</span>
          </span>
          <span class="step-result">
            <strong>{{ traceDuration(step.span.duration_ms) }}</strong>
            <span :class="step.span.status">{{ traceStatusLabels[step.span.status] }}</span>
          </span>
        </component>
        <div v-if="step.hasChildren" class="phase-children">
          <ExecutionSteps :steps="step.children" :number-by-id="numberById" nested />
        </div>
      </component>
    </li>
  </ul>
</template>

<style scoped>
.execution-steps { display: grid; gap: 10px; list-style: none; margin: 0; padding: 0; }
.execution-step { border: 1px solid var(--line); border-radius: var(--radius-control); background: var(--bg); }
.step-summary { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: start; gap: 10px; padding: 12px; }
summary.step-summary { cursor: pointer; list-style: none; border-radius: var(--radius-control); background: var(--canvas); }
summary.step-summary::-webkit-details-marker { display: none; }
summary.step-summary:hover { background: var(--surface); }
.step-number { min-width: 26px; padding: 1px 5px; border-radius: 5px; background: var(--surface); text-align: center; color: var(--muted); font-size: var(--font-small); font-variant-numeric: tabular-nums; }
.phase > .step-summary > .step-number { color: var(--on-accent); background: var(--accent); }
.step-content { min-width: 0; }
.step-heading { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 8px; }
.step-name { font-weight: 500; overflow-wrap: anywhere; }
.phase > .step-summary .step-name { font-weight: 600; }
.phase-label, .step-time, .step-result > span { color: var(--muted); font-size: var(--font-caption); }
.step-time { display: block; margin-top: 4px; font-variant-numeric: tabular-nums; }
.time-separator { position: absolute; width: 1px; height: 1px; clip-path: inset(50%); }
.step-toggle { margin-left: auto; color: var(--muted); transition: transform var(--duration-fast); }
.phase[open] > .step-summary .step-toggle { transform: rotate(90deg); }
.step-result { display: grid; justify-items: end; gap: 3px; font-variant-numeric: tabular-nums; white-space: nowrap; }
.step-result strong { font-size: var(--font-small); font-weight: 600; }
.step-result .error { color: var(--danger); }
.step-result .cancelled { color: var(--warning); }
.step-parallel, .step-missing { display: block; margin: 5px 0 0; font-size: var(--font-caption); overflow-wrap: anywhere; }
.step-parallel { color: var(--link); }
.step-missing { color: var(--warning); }
.phase-children { margin: 0 12px 12px 24px; padding: 4px 0 0 10px; border-left: 2px solid var(--line); }
.nested { gap: 2px; }
.nested > li > .execution-step { border: 0; border-radius: 0; }
.nested > li + li { border-top: 1px solid var(--line); }
.nested .step-summary { padding: 10px 4px; gap: 8px; }
.nested .step-number { background: none; color: var(--muted); font-size: var(--font-caption); }
@media (max-width: 520px) {
  .step-summary { padding: 10px; gap: 8px; }
  .phase-children { margin-left: 14px; margin-right: 6px; padding-left: 6px; }
  .step-result { grid-column: 2 / -1; display: flex; align-items: baseline; gap: 8px; }
  .step-content { grid-column: 2 / -1; }
  .step-number { min-width: 24px; }
  .step-time { line-height: 1.7; }
}
</style>
