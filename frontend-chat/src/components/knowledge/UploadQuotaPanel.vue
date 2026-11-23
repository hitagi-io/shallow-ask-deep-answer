<script setup lang="ts">
import { computed } from 'vue'
import type { UploadQuota } from '../../api'
import { formatQuotaBytes } from '../../utils/uploadQuota'
import AppIcon from '../common/AppIcon.vue'

const props = defineProps<{ quota: UploadQuota | null; loading: boolean; error: string }>()
defineEmits<{ refresh: [] }>()

const count = new Intl.NumberFormat('zh-CN')
const resetTime = new Intl.DateTimeFormat('zh-CN', {
  month: 'long', day: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
})
const metrics = [
  { key: 'daily_bytes', label: '每日上传量', format: formatQuotaBytes, hint: '请在每日额度重置后再上传。' },
  { key: 'embedding_chars', label: '每日入库字符数', format: count.format, hint: '上传、补建和修复共享此额度，重置后可继续入库。' },
  { key: 'storage_bytes', label: '存储空间', format: formatQuotaBytes, hint: '删除不再需要的资料可释放空间。' },
  { key: 'pending_tasks', label: '待处理上传任务', format: count.format, hint: '名额已满，现有任务完成后可继续上传。' },
] as const

const rows = computed(() => {
  const quota = props.quota
  if (!quota) return []
  return metrics.map(({ key, label, format, hint }) => {
    const used = quota.usage[key]
    const limit = quota.limits[key]
    const remaining = Math.max(0, limit - used)
    const percent = remaining * 100 / limit
    const balance = percent > 0 && percent < 1 ? '<1%' : `${Math.floor(percent)}%`
    return { key, label, used: format(used), limit: format(limit), percent, balance,
      tone: remaining === 0 ? 'exhausted' : percent <= 20 ? 'low' : '',
      hint: remaining === 0 ? hint : '' }
  })
})
</script>

<template>
  <section class="quota-panel" aria-labelledby="upload-quota-title" :aria-busy="loading">
    <header class="quota-heading">
      <div><h3 id="upload-quota-title">剩余上传额度</h3><span>账号内所有项目共享</span></div>
      <button type="button" class="icon-button" :disabled="loading" aria-label="刷新上传额度" title="刷新上传额度" @click="$emit('refresh')">
        <AppIcon name="refresh" :size="16" />
      </button>
    </header>
    <p v-if="error" class="quota-error" role="status">{{ quota ? '额度更新失败，当前显示上次用量。' : '暂时无法读取额度。' }}{{ error }}</p>
    <p v-else-if="!quota" class="quota-placeholder" role="status">正在读取额度…</p>
    <template v-if="quota">
      <div class="quota-grid">
        <div v-for="row in rows" :key="row.key" class="quota-item" :class="row.tone">
          <div class="quota-ring" role="meter" :aria-label="`${row.label}剩余额度`" aria-valuemin="0" aria-valuemax="100"
            :aria-valuenow="row.percent" :aria-valuetext="`剩余 ${row.balance}，已用 ${row.used}，上限 ${row.limit}`"
            :style="{ '--remaining': `${row.percent}%` }">
            <span aria-hidden="true">{{ row.balance }}</span>
          </div>
          <div class="quota-detail">
            <strong>{{ row.label }}</strong>
            <p>已用 {{ row.used }} / {{ row.limit }}</p>
          </div>
          <p v-if="row.hint" class="quota-hint">{{ row.hint }}</p>
        </div>
      </div>
      <footer>
        <span>每日额度重置于 <time :datetime="quota.usage.resets_at">{{ resetTime.format(new Date(quota.usage.resets_at)) }}</time>（本地时间）</span>
        <span>单次请求上限 {{ formatQuotaBytes(quota.limits.request_bytes) }}（含表单开销）</span>
        <span v-if="quota.usage.reserved_bytes">上传量和存储用量含接收中的 {{ formatQuotaBytes(quota.usage.reserved_bytes) }} 预留空间。</span>
      </footer>
    </template>
  </section>
</template>

<style scoped>
.quota-panel { container-type: inline-size; margin-top: 12px; padding: 14px 20px 12px; border: 1px solid var(--line); border-radius: var(--radius-card); }
.quota-heading, .quota-heading > div { display: flex; align-items: center; gap: 8px; }
.quota-heading { justify-content: space-between; }
.quota-heading > div { flex-wrap: wrap; gap: 4px 12px; }
.quota-heading h3 { margin: 0; font-size: var(--font-subheading); font-weight: 600; }
.quota-heading span, .quota-item p, footer, .quota-placeholder, .quota-error { font-size: var(--font-caption); color: var(--muted); }
.quota-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); align-items: start; gap: 16px 24px; margin: 12px 0 16px; }
.quota-item { display: grid; grid-template-columns: auto minmax(0, 1fr); align-items: center; gap: 4px 10px; --meter-color: var(--accent); }
.quota-item.low { --meter-color: var(--warning); }
.quota-item.exhausted { --meter-color: var(--danger); }
.quota-ring { display: grid; place-items: stretch; width: 44px; height: 44px; padding: 3px; border-radius: 50%; background: conic-gradient(var(--meter-color) var(--remaining), var(--line) 0); }
.quota-ring span { display: grid; place-items: center; border-radius: inherit; background: var(--bg); color: var(--meter-color); font-size: var(--font-caption); font-weight: 600; }
.quota-detail strong { font-size: var(--font-small); font-weight: 500; }
.quota-item p { margin: 0; overflow-wrap: anywhere; }
.quota-item .quota-hint, .quota-error { color: var(--danger); }
.quota-hint { grid-column: 1 / -1; }
.quota-ring, .quota-item p, time { font-variant-numeric: tabular-nums; }
footer { display: flex; flex-wrap: wrap; gap: 4px 18px; padding-top: 10px; border-top: 1px solid var(--line); }
.quota-placeholder, .quota-error { margin: 8px 0; }
@container (min-width: 860px) { .quota-grid { grid-template-columns: repeat(4, minmax(0, 1fr)); } }
@container (max-width: 440px) { .quota-grid { grid-template-columns: 1fr; } }
@media (max-width: 600px) { .quota-panel { padding-inline: 14px; } }
</style>
