import { ref, computed, watch, onMounted, onUnmounted, type Ref } from 'vue'
import { fetchFiles, fetchUploadTasks, uploadFiles, deleteFile, buildKnowledgeIndex, fetchIndexHealth, repairIndexHealth, isRequestCancelled, type FileItem, type UploadTask } from '../api'
import { useNotice } from './useNotice'
import { useHealthReport } from './useHealthReport'
import type { ConfirmAction } from './useConfirmation'
import { knowledgeFileTypes, supportsKnowledgeFile } from '../utils/knowledgeFormats'

type Operation = '' | 'loading' | 'uploading' | 'building' | 'checking' | 'repairing' | 'deleting'
interface UploadResult { name: string; status: 'waiting' | 'uploading' | 'queued' | 'error'; detail: string }

export function useKnowledge(projectId: Readonly<Ref<string | null>>, active: Readonly<Ref<boolean>>, confirmAction: ConfirmAction) {
  let disposed = false
  const uploadAbort = new AbortController()
  let readAbort = new AbortController()
  let viewVersion = 0
  let filesRequestVersion = 0
  let tasksRequestVersion = 0
  let lastFullRefresh = 0
  let pollTimer: ReturnType<typeof setTimeout> | undefined
  const files = ref<FileItem[]>([])
  const healthState = useHealthReport()
  const { health, healthNotice, indexStatus } = healthState
  const operation = ref<Operation>('')
  const deletingName = ref('')
  const dragOver = ref(false)
  const fileInput = ref<HTMLInputElement | null>(null)
  const search = ref('')
  const statusFilter = ref<'all' | 'indexed' | 'pending'>('all')
  const uploadResults = ref<UploadResult[]>([])
  const uploadTasks = ref<UploadTask[]>([])
  const taskLoadError = ref('')
  const indexing = computed(() => uploadTasks.value.some(task => task.status === 'queued' || task.status === 'running'))
  const taskCompleted = computed(() => uploadTasks.value.reduce((sum, task) => sum + task.completed, 0))
  const taskTotal = computed(() => uploadTasks.value.reduce((sum, task) => sum + task.total, 0))
  function taskFileDetail(file: UploadTask['files'][number]) {
    if (file.error) return file.error
    return { queued: '排队等待入库', running: '正在解析和索引', success: file.overwritten ? '已覆盖并入库' : '已入库', error: '入库失败，请重新上传' }[file.status]
  }
  const uploadProgress = computed(() => uploadResults.value.filter(result =>
    result.status === 'waiting' || result.status === 'uploading'))
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
  const documentStatus = computed(() => {
    if (loadError.value) return { label: '状态未知', tone: 'warning' }
    if (knowledgeLoading.value) return { label: '加载中', tone: 'neutral' }
    if (!files.value.length) return { label: '暂无资料', tone: 'neutral' }
    const indexed = files.value.filter(file => file.indexed).length
    if (!indexed) return { label: '待入库', tone: 'warning' }
    return indexed === files.value.length
      ? { label: '已入库', tone: 'success' } : { label: '部分入库', tone: 'warning' }
  })
  const filteredFiles = computed(() => files.value.filter(file =>
    file.name.toLocaleLowerCase().includes(search.value.trim().toLocaleLowerCase()) &&
    (statusFilter.value === 'all' || file.indexed === (statusFilter.value === 'indexed'))))
  const errorText = (error: unknown) => error instanceof Error ? error.message : '操作失败，请稍后重试'

  function cancelReads() {
    readAbort.abort()
    readAbort = new AbortController()
    filesRequestVersion++
    tasksRequestVersion++
  }

  // 所有操作入口共用一把同步锁，拖放和键盘操作也不例外。
  async function run(kind: Operation, action: (id: string) => Promise<void>) {
    if (disposed || !projectId.value || knowledgeBusy.value) return
    const id = projectId.value
    const view = viewVersion
    // 前台操作开始后，忽略正在执行的旧轮询结果及其错误。
    healthState.beginRequest()
    cancelReads()
    operation.value = kind
    clearNotice()
    try { await action(id) }
    catch (error) { if (!isRequestCancelled(error) && !disposed && viewVersion === view) show(errorText(error), 'error') }
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
      const report = await fetchIndexHealth(id, refresh, { signal: readAbort.signal })
      return !disposed && projectId.value === id && healthState.acceptReport(request, report)
    } catch (error) {
      if (!isRequestCancelled(error) && !disposed && projectId.value === id) healthState.rejectReport(request, errorText(error))
      throw error
    }
  }
  async function refreshTasks(id: string) {
    if (disposed || projectId.value !== id) return
    const request = ++tasksRequestVersion
    try {
      const result = await fetchUploadTasks(id, { signal: readAbort.signal })
      if (!disposed && request === tasksRequestVersion && projectId.value === id) {
        uploadTasks.value = result
        taskLoadError.value = ''
      }
    } catch (error) {
      if (!isRequestCancelled(error) && !disposed && request === tasksRequestVersion && projectId.value === id) taskLoadError.value = errorText(error)
      throw error
    }
  }
  async function refreshAll(id: string) {
    const results = await Promise.allSettled([refreshFiles(id), refreshHealth(id), refreshTasks(id)])
    const failed = results.find(result => result.status === 'rejected')
    if (failed?.status === 'rejected') throw failed.reason
  }
  function loadKnowledgeState() { return run('loading', refreshAll) }
  function selectFiles() { if (!knowledgeBusy.value) fileInput.value?.click() }

  async function handleUpload(fileList: FileList | File[]) {
    if (!fileList.length) return
    await run('uploading', async id => {
      uploadResults.value = Array.from(fileList).map(file => ({ name: file.name, status: 'waiting', detail: '等待上传' }))
      const rows = uploadResults.value
      const picked = Array.from(fileList)
      for (let index = 0; index < picked.length && !disposed; index++) {
        const file = picked[index]
        const row = rows[index]
        if (!supportsKnowledgeFile(file.name)) { row.status = 'error'; row.detail = `支持的文件类型：${knowledgeFileTypes}`; continue }
        row.status = 'uploading'; row.detail = '正在上传'
        try {
          const result = await uploadFiles(id, [file], () => { row.detail = '传输完成，正在保存' }, uploadAbort.signal)
          const error = result.errors[0]?.error
          row.status = result.task_id && !error ? 'queued' : 'error'
          row.detail = error || (result.task_id ? '已保存，等待后台入库' : '上传未被接受')
        } catch (error) { row.status = 'error'; row.detail = errorText(error) }
      }
      if (!disposed) {
        const failures = rows.filter(result => result.status === 'error')
        const succeeded = rows.length - failures.length
        const summary = failures.length
          ? `已提交 ${succeeded} 个文件，上传失败 ${failures.length} 个`
          : `已保存 ${succeeded} 个文件，后台将自动入库，可继续上传或离开页面`
        const details = failures.map(result => `${result.name}：${result.detail}`)
        uploadResults.value = []
        if (projectId.value === id) healthState.reset()
        // 刷新异常与上传结果合并，避免覆盖具体文件的失败原因。
        try { await refreshAll(id) }
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
    if (knowledgeBusy.value || indexing.value || !await confirmAction(`确定删除 ${name}？`)) return
    await run('deleting', async id => {
      deletingName.value = name
      await deleteFile(id, name)
      if (projectId.value === id) healthState.reset()
      await refreshAll(id)
      show(`已删除：${name}`, 'success')
    })
  }
  function handleBuild() {
    if (indexing.value) return
    return run('building', async id => {
      const result = await buildKnowledgeIndex(id)
      if (projectId.value === id) healthState.reset()
      await refreshAll(id)
      const errors = result.failures.map(f => `${f.file.split(/[\\/]/).pop()}：${f.error}`)
      show([result.message, ...errors].join('；'), errors.length ? 'error' : 'success')
    })
  }
  function handleHealthCheck() {
    if (indexing.value) return
    return run('checking', async id => { await refreshHealth(id, true); await refreshFiles(id) })
  }
  async function handleHealthRepair() {
    if (knowledgeBusy.value || indexing.value || !await confirmAction('立即修复索引？将清理残留向量，并重建分块缺失、损坏或内容陈旧的索引。')) return
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
      const errors = [...repair.ghost_errors, ...repair.re_index_failures.map(f => `${f.file}：${f.error}`)]
      show(errors.length ? `修复部分失败：${errors.join('；')}` : report.healthy ? '修复完成，索引状态良好' : '修复已执行，仍有异常，请查看检查结果', errors.length || !report.healthy ? 'error' : 'success')
    })
  }
  // 可见项目只轮询文件元数据和缓存报告，不启动完整性扫描。
  async function poll() {
    if (disposed) return
    try {
      const id = projectId.value
      if (active.value && id && !knowledgeBusy.value) {
        const before = uploadTasks.value.map(task => `${task.task_id}:${task.status}:${task.completed}`).join('|')
        await refreshTasks(id)
        const after = uploadTasks.value.map(task => `${task.task_id}:${task.status}:${task.completed}`).join('|')
        if (before !== after || Date.now() - lastFullRefresh > 60_000) {
          await Promise.allSettled([refreshFiles(id), refreshHealth(id)])
          lastFullRefresh = Date.now()
        }
      }
    } catch { /* 读取失败由各自的状态提示展示；主动取消不提示。 */ }
    finally {
      if (!disposed) pollTimer = setTimeout(poll, indexing.value ? 2_000 : 5_000)
    }
  }
  watch(projectId, () => {
    viewVersion++; cancelReads()
    files.value = []; uploadTasks.value = []; uploadResults.value = []; taskLoadError.value = ''; lastFullRefresh = 0; healthState.reset(); search.value = ''; statusFilter.value = 'all'; loadError.value = ''; clearNotice()
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

  return { uploadTasks, indexing, taskLoadError, taskCompleted, taskTotal, taskFileDetail, files, filteredFiles, search, statusFilter, uploadProgress, uploadCompleted, loadError, selectFiles, clearNotice,
    knowledgeLoading, knowledgeBuilding, knowledgeUploading, knowledgeMsg, knowledgeMsgType, dragOver, fileInput,
    deletingName, health, healthNotice, documentStatus, indexStatus, healthChecking, healthRepairing, healthIssueCount, knowledgeBusy, loadKnowledgeState, onFileSelect,
    onDrop, handleDeleteFile, handleBuild, handleHealthCheck, handleHealthRepair, formatHealthTime }
}
