<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { apiGenerationTrace } from '../../api'
import type { TraceDetail } from '../../utils/generation'
import { buildTraceTimeline, traceDuration, traceStatusLabels, type TraceTimeline } from '../../utils/traceTimeline'
import { copyText } from '../../utils/clipboard'
import AppDialog from '../common/AppDialog.vue'
import ExecutionSteps from './ExecutionSteps.vue'

const props = defineProps<{ generationId: number }>()
const emit = defineEmits<{ close: [] }>()
const detail = ref<TraceDetail | null>(null)
const timeline = ref<TraceTimeline | null>(null)
const numberById = computed(() => Object.fromEntries((timeline.value?.rows ?? []).map(row => [row.span.span_id, row.stepNumber])))
const error = ref('')
const loading = ref(false)
const copied = ref(false)
const results: Record<string, string> = { answered: '已回答', clarification: '等待补充问题', no_evidence: '没有足够资料',
  answer_rejected: '回答未通过校验', dependency_failed: '检索服务故障' }
async function load() {
  if (loading.value) return
  loading.value = true; error.value = ''
  try {
    const next = await apiGenerationTrace(props.generationId)
    const nextTimeline = buildTraceTimeline(next.spans)
    detail.value = next; timeline.value = nextTimeline
  } catch (e) {
    detail.value = null; timeline.value = null
    error.value = e instanceof Error ? e.message : '无法读取执行记录'
  }
  finally { loading.value = false }
}
async function copyId() {
  if (!detail.value?.trace_id) return
  try { await copyText(detail.value.trace_id); copied.value = true }
  catch { error.value = '复制失败，请手动复制问题编号' }
}
onMounted(load)
</script>

<template>
  <AppDialog title="执行过程" @close="emit('close')">
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="loading" role="status">正在读取…</p>
    <template v-if="detail && timeline">
      <div class="execution-total" role="group" aria-label="生成总耗时">
        <div>
          <span class="total-label">总耗时</span>
          <strong>{{ timeline.total ? traceDuration(timeline.total.duration_ms) : '暂无完整记录' }}</strong>
          <span v-if="timeline.total" class="step-status" :class="timeline.total.status">{{ traceStatusLabels[timeline.total.status] }}</span>
        </div>
        <p>整次生成从开始到结束的实际耗时。<template v-if="detail.result_kind">回答结果：{{ results[detail.result_kind] }}。</template></p>
      </div>
      <p v-if="!detail.spans.length">暂无执行步骤记录，记录可能仍在写入或已超过保留期限。</p>
      <template v-if="timeline.rows.length">
        <div class="steps-heading">
          <h3>执行步骤</h3>
          <span>点击阶段展开或收起</span>
        </div>
        <p class="execution-hint">同级按开始时间排列，阶段耗时已包含子步骤。时间从{{ timeline.startsWithGeneration ? '本次生成开始' : '首条记录开始' }}起算。</p>
        <ExecutionSteps :steps="timeline.roots" :number-by-id="numberById" />
      </template>
      <p v-else-if="timeline.total" class="execution-hint">暂无子步骤记录。</p>
      <p v-if="!timeline.total && detail.spans.length" class="execution-hint">总耗时记录尚未写入或已缺失，仅展示已收到的步骤。</p>
      <p v-if="detail.truncated" class="execution-hint">步骤较多，当前仅显示前 500 条，包含关系和并行标记可能不完整。</p>
      <p v-if="detail.error_code">错误原因：{{ detail.error_code }}</p>
      <details v-if="detail.trace_id" class="execution-diagnostics">
        <summary>排查信息</summary>
        <p class="execution-id">问题编号：{{ detail.trace_id }}</p>
      </details>
    </template>
    <div class="dialog-actions execution-actions">
      <button class="btn btn-secondary" :disabled="loading" @click="load">刷新</button>
      <button v-if="detail?.trace_id" class="btn btn-secondary" @click="copyId">{{ copied ? '已复制' : '复制问题编号' }}</button>
      <button class="btn btn-primary" @click="emit('close')">关闭</button>
    </div>
  </AppDialog>
</template>

<style scoped>
.execution-total { padding: 14px 16px; border: 1px solid var(--line); border-radius: var(--radius-card); background: var(--canvas); }
.execution-total > div { display: flex; flex-wrap: wrap; align-items: baseline; gap: 8px 12px; }
.total-label { font-weight: 600; }
.execution-total strong { font-size: var(--font-title); font-variant-numeric: tabular-nums; }
.execution-total p { margin: 4px 0 0; color: var(--muted); font-size: var(--font-caption); }
.steps-heading { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 12px; margin-top: 18px; }
.steps-heading h3 { margin: 0; font-size: var(--font-body); }
.steps-heading > span { color: var(--muted); font-size: var(--font-caption); }
.execution-hint { margin: 6px 0 12px; color: var(--muted); font-size: var(--font-caption); }
.step-status.error { color: var(--danger); }
.step-status.cancelled { color: var(--warning); }
.execution-total .step-status.ok { color: var(--success); }
.execution-diagnostics { margin-top: 16px; color: var(--muted); font-size: var(--font-caption); }
.execution-diagnostics summary { cursor: pointer; }
.execution-id { margin: 4px 0 0; overflow-wrap: anywhere; color: var(--muted); font-size: var(--font-caption); }
.execution-actions { position: sticky; bottom: -24px; z-index: 1; margin-top: 16px; padding: 12px 0 4px; border-top: 1px solid var(--line); background: var(--bg); }
@media (max-width: 520px) {
  .execution-total { padding: 12px; }
  .execution-actions { bottom: -16px; }
}
</style>
