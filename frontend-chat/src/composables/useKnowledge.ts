import { ref, computed, watch, onMounted, onUnmounted, type Ref } from 'vue'
import { fetchFiles, uploadFiles, deleteFile, buildKnowledgeIndex, fetchIndexHealth, repairIndexHealth, type FileItem, type HealthReport } from '../api'
import { useNotice } from './useNotice'
import type { ConfirmAction } from './useConfirmation'

type Operation = '' | 'loading' | 'uploading' | 'building' | 'checking' | 'repairing' | 'deleting'
interface UploadResult { name: string; status: 'waiting' | 'uploading' | 'indexing' | 'success' | 'error'; detail: string }

export function useKnowledge(isAdmin: Readonly<Ref<boolean>>, active: Readonly<Ref<boolean>>, confirmAction: ConfirmAction = async () => false) {
  let disposed = false
  const uploadAbort = new AbortController()
  let pollTimer: ReturnType<typeof setTimeout> | undefined
  const files = ref<FileItem[]>([])
  const health = ref<HealthReport | null>(null)
  const operation = ref<Operation>('')
  const deletingName = ref('')
  const dragOver = ref(false)
  const fileInput = ref<HTMLInputElement | null>(null)
  const search = ref('')
  const statusFilter = ref<'all' | 'indexed' | 'pending'>('all')
  const uploadResults = ref<UploadResult[]>([])
  const loadError = ref('')
  const { message: knowledgeMsg, type: knowledgeMsgType, show, clear: clearNotice } = useNotice(0)
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

  // All entry points share one synchronous lock, including drag/drop and keyboard input.
  async function run(kind: Operation, action: () => Promise<void>) {
    if (disposed || !isAdmin.value || knowledgeBusy.value) return
    operation.value = kind
    clearNotice()
    try { await action() }
    catch (error) { show(errorText(error), 'error') }
    finally { operation.value = ''; deletingName.value = '' }
  }
  async function refreshFiles() {
    if (disposed) return
    try {
      const result = await fetchFiles()
      if (!disposed) { files.value = result; loadError.value = '' }
    } catch (error) { loadError.value = errorText(error); throw error }
  }
  async function refreshHealth(refresh = false) {
    if (disposed) return
    const report = await fetchIndexHealth(refresh)
    if (!disposed) health.value = report
  }
  async function refreshAll(force = false) {
    const results = await Promise.allSettled([refreshFiles(), refreshHealth(force)])
    const failed = results.find(result => result.status === 'rejected')
    if (failed?.status === 'rejected') throw failed.reason
  }
  function loadKnowledgeState() { return run('loading', refreshAll) }
  function selectFiles() { if (!knowledgeBusy.value) fileInput.value?.click() }

  async function handleUpload(fileList: FileList | File[]) {
    if (!fileList.length) return
    await run('uploading', async () => {
      uploadResults.value = Array.from(fileList).map(file => ({ name: file.name, status: 'waiting', detail: '等待上传' }))
      const picked = Array.from(fileList)
      for (let index = 0; index < picked.length && !disposed; index++) {
        const file = picked[index]
        const row = uploadResults.value[index]
        if (!/\.(txt|pdf|docx)$/i.test(file.name)) { row.status = 'error'; row.detail = '仅支持 TXT、PDF、DOCX'; continue }
        row.status = 'uploading'; row.detail = '正在上传'
        try {
          const result = await uploadFiles([file], () => { row.status = 'indexing'; row.detail = '上传完成，正在解析和索引' }, uploadAbort.signal)
          const uploaded = result.uploaded[0]
          const error = result.errors[0]?.error || uploaded?.error
          row.status = uploaded?.indexed && !error ? 'success' : 'error'
          row.detail = error || (uploaded?.indexed ? (uploaded.overwritten ? '已覆盖并入库' : '已入库') : '入库失败')
        } catch (error) { row.status = 'error'; row.detail = errorText(error) }
      }
      if (!disposed) await refreshAll(true)
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
    await run('deleting', async () => {
      deletingName.value = name
      await deleteFile(name)
      await refreshAll(true)
      show(`已删除：${name}`, 'success')
    })
  }
  function handleBuild() {
    return run('building', async () => {
      const result = await buildKnowledgeIndex()
      await refreshAll(true)
      const errors = result.failures.map(f => `${f.file.split(/[\\/]/).pop()}：${f.error}`)
      show([result.message, ...errors].join('；'), errors.length ? 'error' : 'success')
    })
  }
  function handleHealthCheck() {
    return run('checking', async () => { await refreshHealth(true); await refreshFiles() })
  }
  async function handleHealthRepair() {
    if (knowledgeBusy.value || !await confirmAction('立即修复索引？将清理残留向量、重建记录并重新索引缺失文件。')) return
    await run('repairing', async () => {
      const report = await repairIndexHealth()
      if (disposed) return
      health.value = report
      await refreshFiles()
      const repair = report.repair
      const errors = [...(repair?.ghost_errors || []), ...(repair?.re_index_failures || []).map(f => `${f.file}：${f.error}`)]
      show(errors.length ? `修复部分失败：${errors.join('；')}` : report.healthy ? '修复完成，索引状态良好' : '修复已执行，仍有异常，请查看检查结果', errors.length || !report.healthy ? 'error' : 'success')
    })
  }
  // Schedule after completion so a slow request never overlaps the next poll.
  async function poll() {
    if (disposed || !isAdmin.value) return
    if (!knowledgeBusy.value) {
      const snapshot = health.value
      try {
        const report = await fetchIndexHealth()
        if (!disposed && !knowledgeBusy.value && health.value === snapshot) {
          const changed = JSON.stringify(report.issues) !== JSON.stringify(snapshot?.issues) || JSON.stringify(report.stats) !== JSON.stringify(snapshot?.stats)
          health.value = report
          if (active.value && changed) await run('loading', refreshFiles)
        }
      } catch { /* Manual refresh exposes errors; polling keeps the last known report. */ }
    }
    if (!disposed) pollTimer = setTimeout(poll, 60_000)
  }
  watch(active, visible => { if (visible) void loadKnowledgeState() }, { immediate: true })
  onMounted(() => { if (isAdmin.value) void poll() })
  onUnmounted(() => { disposed = true; clearTimeout(pollTimer); uploadAbort.abort() })
  function formatHealthTime(iso: string | null) { return iso ? new Date(iso).toLocaleString() : '从未' }

  return { files, filteredFiles, search, statusFilter, uploadResults, loadError, selectFiles, clearNotice,
    knowledgeLoading, knowledgeBuilding, knowledgeUploading, knowledgeMsg, knowledgeMsgType, dragOver, fileInput,
    deletingName, health, healthChecking, healthRepairing, healthIssueCount, knowledgeBusy, loadKnowledgeState, onFileSelect,
    onDrop, handleDeleteFile, handleBuild, handleHealthCheck, handleHealthRepair, formatHealthTime }
}
