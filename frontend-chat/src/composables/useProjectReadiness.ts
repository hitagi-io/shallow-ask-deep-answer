import { computed, ref, watch, type Ref } from 'vue'
import { fetchFileSummary, fetchUploadTasks } from '../api'

type Readiness = 'checking' | 'updating' | 'recovery' | 'empty' | 'partial' | 'ready' | 'error'

// 先核对服务端活动状态，项目空闲时再读取当前资料统计。
export function useProjectReadiness(projectId: Readonly<Ref<string | null>>, updatingProjectId: Readonly<Ref<string | null>>) {
  const remoteStatus = ref<Readiness>('checking')
  const locallyUpdating = computed(() => !!projectId.value && projectId.value === updatingProjectId.value)
  const status = computed<Readiness>(() => !projectId.value ? 'ready' : locallyUpdating.value ? 'updating' : remoteStatus.value)
  const blocked = computed(() => status.value !== 'ready' && status.value !== 'partial')
  const notice = computed(() => {
    switch (status.value) {
      case 'checking': return '正在读取资料状态…'
      case 'updating': return '资料上传或入库中，完成后可发送'
      case 'recovery': return '上传结果待确认，请在知识库执行“修复与补建”'
      case 'empty': return '暂无已入库资料，请在知识库查看'
      case 'partial': return '部分资料未入库，仅使用已入库文件回答'
      case 'error': return '资料状态读取失败'
      default: return ''
    }
  })
  let projectSignal: AbortSignal
  let pending: Promise<boolean> | undefined

  function refresh(): Promise<boolean> {
    const id = projectId.value
    const signal = projectSignal
    if (signal.aborted) return Promise.resolve(false)
    if (!id) return Promise.resolve(true)
    if (locallyUpdating.value) return Promise.resolve(false)
    if (pending) return pending
    pending = (async () => {
      try {
        const { activity } = await fetchUploadTasks(id, { signal })
        if (signal.aborted) return false
        if (activity !== 'idle') {
          remoteStatus.value = activity
          return false
        }
        const summary = await fetchFileSummary(id, { signal })
        if (signal.aborted) return false
        remoteStatus.value = summary.indexed === 0 ? 'empty'
          : summary.indexed < summary.total ? 'partial' : 'ready'
        return !blocked.value
      } catch (reason) {
        if (signal.aborted) return false
        console.error('[读取项目入库状态失败]', reason)
        remoteStatus.value = 'error'
        return false
      } finally {
        if (!signal.aborted) pending = undefined
      }
    })()
    return pending
  }

  watch([projectId, locallyUpdating], ([id, updating], _, onCleanup) => {
    const controller = new AbortController()
    const { signal } = controller
    projectSignal = signal
    let timer: ReturnType<typeof setTimeout> | undefined
    onCleanup(() => {
      controller.abort()
      clearTimeout(timer)
      pending = undefined
    })
    remoteStatus.value = 'checking'
    async function poll() {
      await refresh()
      if (!signal.aborted) timer = setTimeout(poll, status.value === 'updating' ? 2_000 : 5_000)
    }
    if (id && !updating) void poll()
  }, { immediate: true, flush: 'sync' })

  return { status, blocked, notice, refresh }
}
