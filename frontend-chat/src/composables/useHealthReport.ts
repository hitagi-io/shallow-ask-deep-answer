import { computed, onScopeDispose, ref } from 'vue'
import type { HealthReport } from '../api'

// 报告时效按实际检查时间判断，不使用缓存响应的接收时间。
export const HEALTH_REPORT_MAX_AGE_MS = 5 * 60_000
type StatusTone = 'neutral' | 'success' | 'warning' | 'error'
interface DisplayStatus { label: string; tone: StatusTone }

export function useHealthReport() {
  const health = ref<HealthReport | null>(null)
  const healthError = ref('')
  const healthExpired = ref(false)
  let requestVersion = 0
  let expiryTimer: ReturnType<typeof setTimeout> | undefined

  function beginRequest() { return ++requestVersion }
  function acceptReport(request: number, report: HealthReport | null) {
    if (request !== requestVersion) return false
    healthError.value = ''
    // 其他 worker 可能尚无缓存；保留本页已取得的报告，并按原检查时间判断过期。
    if (!report) return true
    clearTimeout(expiryTimer)
    health.value = report
    const checkedAt = report.checked_at ? Date.parse(report.checked_at) : NaN
    const remaining = checkedAt + HEALTH_REPORT_MAX_AGE_MS - Date.now()
    healthExpired.value = !Number.isFinite(remaining) || remaining <= 0
    if (!healthExpired.value) expiryTimer = setTimeout(() => { healthExpired.value = true }, remaining)
    return true
  }
  function rejectReport(request: number, message: string) {
    if (request === requestVersion) healthError.value = message
  }
  function reset() {
    beginRequest()
    clearTimeout(expiryTimer)
    health.value = null
    healthError.value = ''
    healthExpired.value = false
  }
  onScopeDispose(() => { beginRequest(); clearTimeout(expiryTimer) })

  const healthNotice = computed(() => {
    if (healthError.value) return `报告读取失败：${healthError.value}。${health.value ? '上次报告仅供参考；' : ''}可稍后重试或点击“检查索引”。`
    if (healthExpired.value) return '报告已过期：最近 5 分钟没有有效的检查结果，当前状态未知。历史报告仅供参考，请重新检查。'
    return ''
  })
  const indexStatus = computed<DisplayStatus>(() => {
    const report = health.value
    if (healthError.value) return { label: '检查失败', tone: 'warning' }
    if (healthExpired.value) return { label: '报告已过期', tone: 'warning' }
    if (!report) return { label: '待检查', tone: 'neutral' }
    if (!report.healthy || report.milvus_error || report.manifest_error) return { label: report.issues.length ? `${report.issues.length} 类问题` : '需要处理', tone: 'error' }
    if (!report.stats.indexed_files) return { label: '暂无已验证索引', tone: 'neutral' }
    return { label: '现有索引完整', tone: 'success' }
  })

  return { health, healthError, healthExpired, healthNotice, indexStatus,
    beginRequest, acceptReport, rejectReport, reset }
}
