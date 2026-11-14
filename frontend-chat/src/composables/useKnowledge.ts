import { ref, computed, watch, onMounted, onUnmounted, type Ref } from 'vue'
import { fetchFiles, fetchUploadTasks, uploadFiles, deleteFile, fetchIndexHealth, repairIndexHealth, isRequestCancelled, type FileItem, type UploadTask } from '../api'
import { useNotice } from './useNotice'
import { useHealthReport } from './useHealthReport'
import type { ConfirmAction } from './useConfirmation'
import { knowledgeFileTypes, supportsKnowledgeFile } from '../utils/knowledgeFormats'
import { fileProgressStageCount, transferredPercent, taskFileDetail, taskFilePercent } from '../utils/uploadProgress'
import type { FileFilter, FileSort, FileSummary } from '../utils/knowledgeFiles'

type Operation = '' | 'loading' | 'uploading' | 'checking' | 'repairing' | 'deleting'
interface UploadResult {
  id: number
  taskId: string | null
  name: string
  status: 'waiting' | 'uploading' | 'saving' | 'queued' | 'error'
  detail: string
  percent: number
}
interface UploadProgressFile {
  name: string
  status: UploadResult['status'] | UploadTask['files'][number]['status']
  detail: string
  percent: number
}
interface UploadProgressGroup { id: string; files: UploadProgressFile[]; error: string | null }

export function useKnowledge(projectId: Readonly<Ref<string | null>>, active: Readonly<Ref<boolean>>, confirmAction: ConfirmAction) {
  let disposed = false
  const uploadAbort = new AbortController()
  let readAbort = new AbortController()
  let filesAbort = new AbortController()
  let filterTimer: ReturnType<typeof setTimeout> | undefined
  let viewVersion = 0
  let filesRequestVersion = 0
  let tasksRequestVersion = 0
  let lastFullRefresh = 0
  let nextUploadId = 0
  let tasksBeforeUpload = new Set<string>()
  let pollTimer: ReturnType<typeof setTimeout> | undefined
  const files = ref<FileItem[]>([])
  const fileSummary = ref<FileSummary | null>(null)
  const fileTotal = computed(() => fileSummary.value?.total ?? 0)
  const indexedTotal = computed(() => fileSummary.value?.indexed ?? 0)
  const matchedFiles = ref(0)
  const filePage = ref(1)
  const filePageCount = ref(0)
  const filesLoading = ref(false)
  const healthState = useHealthReport()
  const { health, healthNotice, indexStatus } = healthState
  const operation = ref<Operation>('')
  const updatingProjectId = ref<string | null>(null)
  const deletingName = ref('')
  const dragOver = ref(false)
  const fileInput = ref<HTMLInputElement | null>(null)
  const search = ref('')
  const statusFilter = ref<FileFilter>('all')
  const fileSort = ref<FileSort>('uploaded_desc')
  const uploadResults = ref<UploadResult[]>([])
  const uploadTasks = ref<UploadTask[]>([])
  const taskLoadError = ref('')
  const indexing = computed(() => uploadTasks.value.some(task => task.status === 'queued' || task.status === 'running'))
  const uploadProgress = computed<UploadProgressGroup[]>(() => {
    // 接收响应前可能已经轮询到新任务；等 taskId 返回后在原条目中接续，避免重复显示。
    const visibleTasks = uploadTasks.value.filter(task => operation.value !== 'uploading'
      || tasksBeforeUpload.has(task.task_id) || uploadResults.value.some(row => row.taskId === task.task_id))
    const localIds = new Map(uploadResults.value.filter(row => row.taskId !== null)
      .map(row => [row.taskId, `upload-${row.id}`]))
    // 已接收的任务沿用服务端的上传时间倒序；本地条目接续时保留原来的 id。
    const groups = visibleTasks.map(task => ({
      id: localIds.get(task.task_id) ?? `task-${task.task_id}`,
      files: task.files.map(file => ({ name: file.name, status: file.status, detail: taskFileDetail(file), percent: taskFilePercent(file) })),
      error: task.error,
    }))
    const taskIds = new Set(visibleTasks.map(task => task.task_id))
    // 尚未出现在服务端快照中的本地上传置顶，后加入的文件排在前面。
    const pending = uploadResults.value.filter(row => row.taskId === null || !taskIds.has(row.taskId)).reverse()
    const pendingGroups = pending.map(result => ({
      id: `upload-${result.id}`, error: null, files: [{
        name: result.name, status: result.status, detail: result.detail,
        percent: result.percent,
      }],
    }))
    return [...pendingGroups, ...groups]
  })
  const loadError = ref('')
  const { message: knowledgeMsg, type: knowledgeMsgType, show, clear: clearNotice } = useNotice(5000)
  const knowledgeBusy = computed(() => operation.value !== '')
  const knowledgeLoading = computed(() => operation.value === 'loading')
  const knowledgeUploading = computed(() => operation.value === 'uploading')
  const healthChecking = computed(() => operation.value === 'checking')
  const healthRepairing = computed(() => operation.value === 'repairing')
  const healthIssueCount = computed(() => health.value?.issues.length || 0)
  const documentStatus = computed(() => {
    if (loadError.value) return { label: '状态未知', tone: 'warning' }
    if (!fileSummary.value) return { label: filesLoading.value ? '加载中' : '状态未知', tone: 'neutral' }
    if (!fileTotal.value) return { label: '暂无资料', tone: 'neutral' }
    if (!indexedTotal.value) return { label: '待入库', tone: 'warning' }
    return indexedTotal.value === fileTotal.value
      ? { label: '已入库', tone: 'success' } : { label: '部分入库', tone: 'warning' }
  })
  const errorText = (error: unknown) => error instanceof Error ? error.message : '操作失败，请稍后重试'

  function cancelFileReads() {
    filesAbort.abort()
    filesAbort = new AbortController()
    filesRequestVersion++
    filesLoading.value = false
  }
  function cancelReads() {
    clearTimeout(filterTimer)
    filterTimer = undefined
    readAbort.abort()
    readAbort = new AbortController()
    cancelFileReads()
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
    if (kind === 'uploading' || kind === 'repairing') updatingProjectId.value = id
    clearNotice()
    try { await action(id) }
    catch (error) { if (!isRequestCancelled(error) && !disposed && viewVersion === view) show(errorText(error), 'error') }
    finally {
      operation.value = ''; deletingName.value = ''; updatingProjectId.value = null
      if (!disposed && active.value && viewVersion !== view) void loadKnowledgeState()
    }
  }
  async function refreshFiles(id: string) {
    if (disposed || projectId.value !== id) return
    cancelFileReads()
    const request = ++filesRequestVersion
    const options = { signal: filesAbort.signal, page: filePage.value, search: search.value.trim(), status: statusFilter.value, sort: fileSort.value }
    filesLoading.value = true
    try {
      const page = await fetchFiles(id, options)
      if (!disposed && request === filesRequestVersion && projectId.value === id) {
        files.value = page.files
        filePage.value = page.page
        filePageCount.value = page.total_pages
        matchedFiles.value = page.matched_total
        fileSummary.value = page.summary
        loadError.value = ''
      }
    } catch (error) {
      if (!isRequestCancelled(error) && !disposed && request === filesRequestVersion && projectId.value === id) loadError.value = errorText(error)
      throw error
    } finally {
      if (request === filesRequestVersion) filesLoading.value = false
    }
  }
  async function goToFilePage(page: number) {
    const id = projectId.value
    if (disposed || !id || knowledgeBusy.value || filesLoading.value || !Number.isInteger(page)
        || page < 1 || page > filePageCount.value || page === filePage.value) return
    filePage.value = page
    files.value = []
    loadError.value = ''
    try { await refreshFiles(id) }
    catch { /* 读取失败已显示在资料列表中，重新加载会重试当前页。 */ }
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
    const submittedBeforeRequest = new Set(uploadResults.value.map(row => row.taskId))
    try {
      const result = await fetchUploadTasks(id, { signal: readAbort.signal })
      if (!disposed && request === tasksRequestVersion && projectId.value === id) {
        uploadTasks.value = result
        // 仅清理请求前已确认的历史任务；请求期间新提交的任务可能尚未进入这次快照。
        const taskIds = new Set(result.map(task => task.task_id))
        uploadResults.value = uploadResults.value.filter(row => row.taskId === null
          || !submittedBeforeRequest.has(row.taskId) || taskIds.has(row.taskId))
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
      tasksBeforeUpload = new Set(uploadTasks.value.map(task => task.task_id))
      uploadResults.value = Array.from(fileList).map(file => ({ id: nextUploadId++, taskId: null, name: file.name, status: 'waiting', detail: '等待上传', percent: 0 }))
      const rows = uploadResults.value
      const picked = Array.from(fileList)
      let next = 0
      const sameName = new Map<string, Promise<void>>()
      async function uploadWorker() {
        while (next < picked.length && !disposed) {
          const index = next++
          const file = picked[index]
          const row = rows[index]
          if (!supportsKnowledgeFile(file.name)) { row.status = 'error'; row.detail = `支持的文件类型：${knowledgeFileTypes}`; continue }
          // 不同文件并发传输；同名覆盖仍按选择顺序提交，含 Windows 大小写等价名。
          const key = file.name.toLowerCase()
          const previous = sameName.get(key)
          let release!: () => void
          const current = new Promise<void>(resolve => { release = resolve })
          sameName.set(key, current)
          try {
            await previous
            if (disposed) return
            row.status = 'uploading'; row.detail = '正在传输'; row.percent = 0
            const result = await uploadFiles(id, [file], {
              signal: uploadAbort.signal,
              onProgress: percent => { if (percent !== null) row.percent = Math.floor(percent / fileProgressStageCount) },
              onUploaded: () => { row.status = 'saving'; row.detail = '正在保存'; row.percent = transferredPercent },
            })
            const error = result.errors[0]?.error
            row.status = result.task_id && !error ? 'queued' : 'error'
            row.taskId = result.task_id
            row.detail = error || (result.task_id ? '排队等待入库' : '上传未被接受')
          } catch (error) { row.status = 'error'; row.detail = errorText(error) }
          finally {
            release()
            if (sameName.get(key) === current) sameName.delete(key)
          }
        }
      }
      await Promise.all(Array.from({ length: Math.min(3, picked.length) }, uploadWorker))
      if (!disposed) {
        const failures = rows.filter(result => result.status === 'error')
        const succeeded = rows.length - failures.length
        const summary = failures.length
          ? `已提交 ${succeeded} 个文件，上传失败 ${failures.length} 个`
          : `已保存 ${succeeded} 个文件，后台将自动入库`
        const details = failures.map(result => `${result.name}：${result.detail}`)
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
  function handleHealthCheck() {
    if (indexing.value) return
    return run('checking', async id => { await refreshHealth(id, true); await refreshFiles(id) })
  }
  async function handleHealthRepair() {
    if (knowledgeBusy.value || indexing.value || !await confirmAction('立即修复并补建索引？将为未入库文件建立索引，修复缺失、损坏或过期的索引，并清理残留分块。')) return
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
      show(errors.length ? `修复与补建部分失败：${errors.join('；')}` : report.healthy ? '修复与补建完成，索引状态良好' : '修复与补建已执行，仍有异常，请查看检查结果', errors.length || !report.healthy ? 'error' : 'success')
    })
  }
  // 可见项目只轮询文件元数据和缓存报告，不启动完整性扫描。
  async function poll() {
    if (disposed) return
    try {
      const id = projectId.value
      if (active.value && id && (!knowledgeBusy.value || knowledgeUploading.value)) {
        const before = uploadTasks.value.map(task => `${task.task_id}:${task.status}:${task.completed}`).join('|')
        await refreshTasks(id)
        const after = uploadTasks.value.map(task => `${task.task_id}:${task.status}:${task.completed}`).join('|')
        if (!knowledgeBusy.value && !filesLoading.value
            && (before !== after || Date.now() - lastFullRefresh > 60_000)) {
          await Promise.allSettled([refreshFiles(id), refreshHealth(id)])
          lastFullRefresh = Date.now()
        }
      }
    } catch { /* 读取失败由各自的状态提示展示；主动取消不提示。 */ }
    finally {
      if (!disposed) pollTimer = setTimeout(poll, indexing.value || knowledgeUploading.value ? 2_000 : 5_000)
    }
  }
  watch(projectId, () => {
    viewVersion++; cancelReads()
    fileSummary.value = null; filePage.value = 1; filePageCount.value = 0; matchedFiles.value = 0
    files.value = []; uploadTasks.value = []; uploadResults.value = []; taskLoadError.value = ''; lastFullRefresh = 0; healthState.reset(); search.value = ''; statusFilter.value = 'all'; fileSort.value = 'uploaded_desc'; loadError.value = ''; clearNotice()
    if (active.value) void loadKnowledgeState()
  }, { flush: 'sync' })
  watch([search, statusFilter, fileSort], () => {
    if (knowledgeBusy.value) return
    clearTimeout(filterTimer)
    filterTimer = undefined
    cancelFileReads()
    files.value = []; filePage.value = 1; filePageCount.value = 0; matchedFiles.value = 0; loadError.value = ''
    const id = projectId.value
    if (!active.value || !id) return
    filesLoading.value = true
    filterTimer = setTimeout(() => {
      filterTimer = undefined
      void refreshFiles(id).catch(() => { /* 读取失败已显示在资料列表中。 */ })
    }, 200)
  })
  watch(active, visible => {
    viewVersion++
    if (visible) void loadKnowledgeState()
    else { cancelReads(); healthState.beginRequest() }
  }, { immediate: true })
  onMounted(() => { void poll() })
  onUnmounted(() => { disposed = true; cancelReads(); clearTimeout(pollTimer); uploadAbort.abort() })
  function formatHealthTime(iso: string | null) { return iso ? new Date(iso).toLocaleString() : '从未' }

  return { updatingProjectId, indexing, taskLoadError, files, fileTotal, indexedTotal, matchedFiles, filePage, filePageCount, filesLoading, goToFilePage, search, statusFilter, fileSort, uploadProgress, loadError, selectFiles, clearNotice,
    knowledgeLoading, knowledgeUploading, knowledgeMsg, knowledgeMsgType, dragOver, fileInput,
    deletingName, health, healthNotice, documentStatus, indexStatus, healthChecking, healthRepairing, healthIssueCount, knowledgeBusy, loadKnowledgeState, onFileSelect,
    onDrop, handleDeleteFile, handleHealthCheck, handleHealthRepair, formatHealthTime }
}
