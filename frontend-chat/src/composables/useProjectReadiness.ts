import { computed, onUnmounted, ref, watch, type Ref } from 'vue'
import { fetchFileSummary, fetchUploadTasks } from '../api'

type Readiness = 'checking' | 'updating' | 'empty' | 'partial' | 'ready' | 'error'

// 只读取文件入库记录和后台任务，不触发索引检查或检索。
export function useProjectReadiness(projectId: Readonly<Ref<string | null>>, updatingProjectId: Readonly<Ref<string | null>>) {
  const remoteStatus = ref<Readiness>('checking')
  const locallyUpdating = computed(() => !!projectId.value && projectId.value === updatingProjectId.value)
  const status = computed<Readiness>(() => !projectId.value ? 'ready' : locallyUpdating.value ? 'updating' : remoteStatus.value)
  const blocked = computed(() => status.value !== 'ready' && status.value !== 'partial')
  const notice = computed(() => {
    switch (status.value) {
      case 'checking': return '正在读取资料状态…'
      case 'updating': return '资料上传或入库中，完成后可发送'
      case 'empty': return '暂无已入库资料，请在知识库查看'
      case 'partial': return '部分资料未入库，仅使用已入库文件回答'
      case 'error': return '资料状态读取失败'
      default: return ''
    }
  })
  let disposed = false
  let version = 0
  let controller: AbortController | undefined
  let pending: Promise<boolean> | undefined
  let timer: ReturnType<typeof setTimeout> | undefined

  function refresh(): Promise<boolean> {
    const id = projectId.value
    if (disposed) return Promise.resolve(false)
    if (!id) return Promise.resolve(true)
    if (locallyUpdating.value) return Promise.resolve(false)
    if (pending) return pending
    const requestVersion = version
    controller = new AbortController()
    const options = { signal: controller.signal }
    pending = (async () => {
      try {
        const [tasks, summary] = await Promise.all([fetchUploadTasks(id, options), fetchFileSummary(id, options)])
        if (disposed || requestVersion !== version) return false
        remoteStatus.value = tasks.some(task => task.status === 'queued' || task.status === 'running') ? 'updating'
          : summary.indexed === 0 ? 'empty'
          : summary.indexed < summary.total ? 'partial' : 'ready'
        return !blocked.value
      } catch (reason) {
        if (disposed || requestVersion !== version) return false
        console.error('[读取项目入库状态失败]', reason)
        remoteStatus.value = 'error'
        return false
      } finally {
        if (requestVersion === version) pending = undefined
      }
    })()
    return pending
  }

  function schedule(requestVersion: number) {
    if (disposed || requestVersion !== version || !projectId.value) return
    clearTimeout(timer)
    timer = setTimeout(async () => {
      await refresh()
      schedule(requestVersion)
    }, status.value === 'updating' ? 2_000 : 5_000)
  }

  watch([projectId, locallyUpdating], () => {
    const requestVersion = ++version
    controller?.abort()
    pending = undefined
    clearTimeout(timer)
    remoteStatus.value = 'checking'
    void refresh().then(() => schedule(requestVersion))
  }, { immediate: true, flush: 'sync' })

  onUnmounted(() => {
    disposed = true
    version++
    controller?.abort()
    clearTimeout(timer)
  })

  return { status, blocked, notice, refresh }
}
