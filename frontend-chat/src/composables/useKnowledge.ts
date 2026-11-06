import { ref, computed, watch, onMounted, onUnmounted, type Ref } from 'vue'
import { fetchFiles, uploadFiles, deleteFile, buildKnowledgeIndex, fetchIndexHealth, repairIndexHealth, isRequestCancelled, type FileItem } from '../api'
import { useNotice } from './useNotice'
import { useHealthReport } from './useHealthReport'
import type { ConfirmAction } from './useConfirmation'

type Operation = '' | 'loading' | 'uploading' | 'building' | 'checking' | 'repairing' | 'deleting'
interface UploadResult { name: string; status: 'waiting' | 'uploading' | 'indexing' | 'success' | 'error'; detail: string }

export function useKnowledge(projectId: Readonly<Ref<string | null>>, active: Readonly<Ref<boolean>>, confirmAction: ConfirmAction = async () => false) {
  let disposed = false
  const uploadAbort = new AbortController()
  let readAbort = new AbortController()
  let viewVersion = 0
  let filesRequestVersion = 0
  let pollTimer: ReturnType<typeof setTimeout> | undefined
  const files = ref<FileItem[]>([])
  const healthState = useHealthReport()
  const { health, healthError, healthExpired, healthNotice, documentStatus, indexStatus } = healthState
  const operation = ref<Operation>('')
  const deletingName = ref('')
  const dragOver = ref(false)
  const fileInput = ref<HTMLInputElement | null>(null)
  const search = ref('')
  const statusFilter = ref<'all' | 'indexed' | 'pending'>('all')
  const uploadResults = ref<UploadResult[]>([])
  const uploadProgress = computed(() => uploadResults.value.filter(result =>
    result.status === 'waiting' || result.status === 'uploading' || result.status === 'indexing'))
  const uploadCompleted = computed(() => uploadResults.value.length - uploadProgress.value.length)
  const loadError = ref('')
  const { message: knowledgeMsg, type: knowledgeMsgType, show, clear: clearNotice } = useNotice(5000)
  const knowledgeBusy = computed(() => operation.value !== '')
  const knowledgeLoading = computed(() => operation.value === 'loading')
  const knowledgeUploading = computed(() => operation.value === 'uploading')
  const knowledgeBuilding = computed(() => operation.value === 'building')
  const healthChecking = computed(() => operation.value === 'checking')
  const healthRepairing = computed(() => operation.value === 'repairing')
  const healthIssueCount = computed(() => health.value?.issues.length || 0)
  const filteredFiles = computed(() => files.value.filter(file =>
    file.name.toLocaleLowerCase().includes(search.value.trim().toLocaleLowerCase()) &&
    (statusFilter.value === 'all' || file.indexed === (statusFilter.value === 'indexed'))))
  const errorText = (error: unknown) => error instanceof Error ? error.message : '操作失败，请稍后重试'

  function cancelReads() {
    readAbort.abort()
    readAbort = new AbortController()
    filesRequestVersion++
  }

  // All entry points share one synchronous lock, including drag/drop and keyboard input.
  async function run(kind: Operation, action: (id: string) => Promise<void>, notify = true) {
    if (disposed || !projectId.value || knowledgeBusy.value) return
    const id = projectId.value
    const view = viewVersion
    // A foreground operation supersedes any poll already in flight, including its errors.
    healthState.beginRequest()
    cancelReads()
    operation.value = kind
    if (notify) clearNotice()
    try { await action(id) }
    catch (error) { if (!isRequestCancelled(error) && notify && !disposed && viewVersion === view) show(errorText(error), 'error') }
    finally {
      operation.value = ''; deletingName.value = ''
      if (!disposed && active.value && viewVersion !== view) void loadKnowledgeState()
    }
  }
  async function refreshFiles(id: string) {
    if (disposed || projectId.value !== id) return
    const request = ++filesRequestVersion
    const signal = readAbort.signal
    try {
      const result = await fetchFiles(id, { signal })
      if (!disposed && request === filesRequestVersion && projectId.value === id) { files.value = result; loadError.value = '' }
    } catch (error) {
      if (!isRequestCancelled(error) && !disposed && request === filesRequestVersion && projectId.value === id) loadError.value = errorText(error)
      throw error
    }
  }
  async function refreshHealth(id: string, refresh = false) {
    if (disposed || projectId.value !== id) return false
    const request = healthState.beginRequest()
    try {
      const report = await fetchIndexHealth(id, refresh || !!healthError.value || healthExpired.value, { signal: readAbort.signal })
      return !disposed && projectId.value === id && healthState.acceptReport(request, report)
    } catch (error) {
      if (!isRequestCancelled(error) && !disposed && projectId.value === id) healthState.rejectReport(request, errorText(error))
      throw error
    }
  }
  async function refreshAll(id: string, force = false) {
    const results = await Promise.allSettled([refreshFiles(id), refreshHealth(id, force)])
    const failed = results.find(result => result.status === 'rejected')
    if (failed?.status === 'rejected') throw failed.reason
  }
  function loadKnowledgeState() { return run('loading', refreshAll) }
  function selectFiles() { if (!knowledgeBusy.value) fileInput.value?.click() }

  async function handleUpload(fileList: FileList | File[]) {
    if (!fileList.length) return
    await run('uploading', async id => {
      uploadResults.value = Array.from(fileList).map(file => ({ name: file.name, status: 'waiting', detail: '等待上传' }))
      const picked = Array.from(fileList)
      for (let index = 0; index < picked.length && !disposed; index++) {
        const file = picked[index]
        const row = uploadResults.value[index]
        if (!/\.(txt|pdf|docx)$/i.test(file.name)) { row.status = 'error'; row.detail = '仅支持 TXT、PDF、DOCX'; continue }
        row.status = 'uploading'; row.detail = '正在上传'
        try {
          const result = await uploadFiles(id, [file], () => { row.status = 'indexing'; row.detail = '上传完成，正在解析和索引' }, uploadAbort.signal)
          const uploaded = result.uploaded[0]
          const error = result.errors[0]?.error || uploaded?.error
          row.status = uploaded?.indexed && !error ? 'success' : 'error'
          row.detail = error || (uploaded?.indexed ? (uploaded.overwritten ? '已覆盖并入库' : '已入库') : '入库失败')
        } catch (error) { row.status = 'error'; row.detail = errorText(error) }
      }
      if (!disposed) {
        const failures = uploadResults.value.filter(result => result.status === 'error')
        const succeeded = uploadResults.value.length - failures.length
        const summary = failures.length
          ? `上传完成：成功 ${succeeded} 个，失败 ${failures.length} 个`
          : `已上传并入库 ${succeeded} 个文件`
        const details = failures.map(result => `${result.name}：${result.detail}`)
        uploadResults.value = []
        // 刷新异常与上传结果合并，避免覆盖具体文件的失败原因。
        try { await refreshAll(id, true) }
        catch (error) { if (!isRequestCancelled(error)) details.push(`刷新状态失败：${errorText(error)}`) }
        if (projectId.value === id) show([summary, ...details].join('\n'), details.length ? 'error' : 'success')
      }
    })
  }
  function onFileSelect(event: Event) {
    const input = event.target as HTMLInputElement
    if (input.files) void handleUpload(input.files)
    input.value = ''
  }
  function onDrop(event: DragEvent) {
    dragOver.value = false
    if (!knowledgeBusy.value && event.dataTransfer?.files) return handleUpload(event.dataTransfer.files)
  }
  async function handleDeleteFile(name: string) {
    if (knowledgeBusy.value || !await confirmAction(`确定删除 ${name}？`)) return
    await run('deleting', async id => {
      deletingName.value = name
      await deleteFile(id, name)
      await refreshAll(id, true)
      show(`已删除：${name}`, 'success')
    })
  }
  function handleBuild() {
    return run('building', async id => {
      const result = await buildKnowledgeIndex(id)
      await refreshAll(id, true)
      const errors = result.failures.map(f => `${f.file.split(/[\\/]/).pop()}：${f.error}`)
      show([result.message, ...errors].join('；'), errors.length ? 'error' : 'success')
    })
  }
  function handleHealthCheck() {
    return run('checking', async id => { await refreshHealth(id, true); await refreshFiles(id) })
  }
  async function handleHealthRepair() {
    if (knowledgeBusy.value || !await confirmAction('立即修复索引？将清理残留向量，并重建分块缺失、损坏或内容陈旧的索引。')) return
    await run('repairing', async id => {
      const request = healthState.beginRequest()
      let report
      try { report = await repairIndexHealth(id) }
      catch (error) {
        if (!disposed && projectId.value === id) healthState.rejectReport(request, errorText(error))
        throw error
      }
      if (disposed || projectId.value !== id) return
      if (!healthState.acceptReport(request, report)) return
      await refreshFiles(id)
      const repair = report.repair
      const errors = [...(repair?.ghost_errors || []), ...(repair?.re_index_failures || []).map(f => `${f.file}：${f.error}`)]
      show(errors.length ? `修复部分失败：${errors.join('；')}` : report.healthy ? '修复完成，索引状态良好' : '修复已执行，仍有异常，请查看检查结果', errors.length || !report.healthy ? 'error' : 'success')
    })
  }
  // Poll only the visible project's report; never apply a previous project's response.
  async function poll() {
    if (disposed) return
    try {
      const id = projectId.value
      if (active.value && id && !knowledgeBusy.value) {
        const snapshot = health.value
        if (await refreshHealth(id)) {
          const report = health.value!
          const changed = JSON.stringify(report.issues) !== JSON.stringify(snapshot?.issues) || JSON.stringify(report.stats) !== JSON.stringify(snapshot?.stats)
          if (changed) await run('loading', refreshFiles, false)
        }
      }
    } catch { /* The persistent health notice marks failures; intentional cancellation stays silent. */ }
    finally {
      if (!disposed) pollTimer = setTimeout(poll, 60_000)
    }
  }
  watch(projectId, () => {
    viewVersion++; cancelReads()
    files.value = []; healthState.reset(); search.value = ''; statusFilter.value = 'all'; loadError.value = ''; clearNotice()
    if (active.value) void loadKnowledgeState()
  }, { flush: 'sync' })
  watch(active, visible => {
    viewVersion++
    if (visible) void loadKnowledgeState()
    else { cancelReads(); healthState.beginRequest() }
  }, { immediate: true })
  onMounted(() => { void poll() })
  onUnmounted(() => { disposed = true; cancelReads(); clearTimeout(pollTimer); uploadAbort.abort() })
  function formatHealthTime(iso: string | null) { return iso ? new Date(iso).toLocaleString() : '从未' }

  return { files, filteredFiles, search, statusFilter, uploadProgress, uploadCompleted, loadError, selectFiles, clearNotice,
    knowledgeLoading, knowledgeBuilding, knowledgeUploading, knowledgeMsg, knowledgeMsgType, dragOver, fileInput,
    deletingName, health, healthNotice, documentStatus, indexStatus, healthChecking, healthRepairing, healthIssueCount, knowledgeBusy, loadKnowledgeState, onFileSelect,
    onDrop, handleDeleteFile, handleBuild, handleHealthCheck, handleHealthRepair, formatHealthTime }
}
