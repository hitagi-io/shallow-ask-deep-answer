<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted, onUnmounted } from 'vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import {
  fetchConversations,
  createConversationApi,
  updateConversationTitle,
  deleteConversationApi,
  streamChat,
  newRequestId,
  getUser,
  clearAuth,
  setUnauthorizedHandler,
  apiLogin,
  apiRegister,
  apiMe,
  fetchFiles,
  uploadFiles,
  deleteFile,
  buildVector,
  fetchIndexHealth,
  repairIndexHealth,
  type Conversation,
  type Message,
  type UserInfo,
  type FileItem,
  type HealthReport,
} from './api'

marked.setOptions({ breaks: true, gfm: true })

// ---------- auth state ----------
const user = ref<UserInfo | null>(getUser())
const authMode = ref<'login' | 'register'>('login')
const authUsername = ref('')
const authPassword = ref('')
const authPassword2 = ref('')
const authError = ref('')
const authSuccess = ref('')
const authLoading = ref(false)

// 在途的流式请求：可被主动取消（401、退出登录、组件卸载、用户点击"停止"），
// 取消后 fetch 连接断开，后端会随之停止生成
let streamAbort: AbortController | null = null

// 用户主动停止生成：中止在途请求，已生成的部分内容保留在气泡里
function stopStreaming() {
  streamAbort?.abort()
}

// 输入框随内容自适应高度；清空内容时需手动复位（程序化清空不会触发 input 事件）
const inputEl = ref<HTMLTextAreaElement | null>(null)
function resetInputHeight() {
  nextTick(() => {
    if (inputEl.value) inputEl.value.style.height = 'auto'
  })
}

// 任意已鉴权接口返回 401（token 过期、账号被删或降权后复核失败）时统一回到登录页，
// 不再依赖各调用点各自处理
setUnauthorizedHandler(() => {
  // 先掐掉在途的流与健康轮询：否则后端仍在生成、定时器还会继续空转
  streamAbort?.abort()
  stopHealthPolling()
  user.value = null
  mode.value = 'chat'
  localStorage.setItem(MODE_KEY, 'chat')
  authMode.value = 'login'
  authError.value = '登录已过期，请重新登录'
})

// ---------- app mode ----------
type AppMode = 'chat' | 'knowledge'
const MODE_KEY = 'deepagent_mode'
function loadMode(): AppMode {
  return localStorage.getItem(MODE_KEY) === 'knowledge' ? 'knowledge' : 'chat'
}
const mode = ref<AppMode>(loadMode())

// ---------- chat state ----------
const conversations = ref<Record<string, Conversation>>({})
const currentId = ref('')
const input = ref('')
const isProcessing = ref(false)
const isThinking = ref(false)
const thinkingSeconds = ref(0)
const thinkingPhase = ref('')
const PHASE_LABELS: Record<string, string> = {
  thinking: '正在思考',
  retrieving: '正在检索知识库',
  searching: '正在联网搜索',
  working: '正在使用工具',
  generating: '正在生成回答',
}
let thinkingTimer: number | null = null
let thinkingStartTime = 0
const chatArea = ref<HTMLElement | null>(null)
const kbOnly = ref(true)
const sidebarOpen = ref(false)
const suggestions = ['总结资料中的关键结论', '查找相关规定与操作流程', '对比不同文档中的观点']
function useSuggestion(text: string) { input.value = text; nextTick(() => inputEl.value?.focus()) }

async function initConversations() {
  if (!user.value) return
  let loaded: Record<string, Conversation> = {}
  try {
    loaded = await fetchConversations()
  } catch {
    loaded = {}
  }
  // 若无会话，由服务端创建一个（会话 id 由服务端生成，本地不再自造 id）
  if (Object.keys(loaded).length === 0) {
    try {
      const conv = await createConversationApi('新对话')
      loaded[conv.id] = conv
    } catch (e) {
      handlePersistError('创建会话', e)
    }
  }
  conversations.value = loaded
  currentId.value = Object.keys(loaded)[0]
}

// 思考计时器
function formatTime(seconds: number): string {
  const m = Math.floor(seconds / 60)
  const s = (seconds % 60).toFixed(1)
  return m > 0 ? `${m}分${s}秒` : `${s}秒`
}

watch(isThinking, (val) => {
  if (val) {
    thinkingSeconds.value = 0
    const tick = () => {
      thinkingSeconds.value = +((Date.now() - thinkingStartTime) / 1000).toFixed(1)
      thinkingTimer = requestAnimationFrame(tick)
    }
    thinkingTimer = requestAnimationFrame(tick)
  } else {
    if (thinkingTimer != null) { cancelAnimationFrame(thinkingTimer); thinkingTimer = null }
  }
})

const current = computed(() => conversations.value[currentId.value])
const convList = computed(() => Object.values(conversations.value))
const hasEmptyConv = computed(() => convList.value.some(c => c.messages.length === 0))

function scrollToBottom() {
  nextTick(() => {
    if (chatArea.value) chatArea.value.scrollTop = chatArea.value.scrollHeight
  })
}

function renderMarkdown(text: string) { return DOMPurify.sanitize(marked.parse(text) as string) }

// 空内容时按终态给出占位文案：服务端只保存模型实际输出，不拼接展示标记
function bubbleText(msg: Message) {
  if (msg.content) return msg.content
  if (msg.status === 'error') return '（生成失败）'
  if (msg.status === 'cancelled') return '（已取消）'
  if (msg.status === 'completed') return '（未收到回复）'
  return ''
}

async function newConversation() {
  if (isProcessing.value) return
  try {
    const conv = await createConversationApi('新对话')
    conversations.value[conv.id] = conv
    currentId.value = conv.id
  } catch (e) {
    handlePersistError('创建会话', e)
  }
}

function selectConversation(id: string) {
  if (isProcessing.value) return
  currentId.value = id
  sidebarOpen.value = false
}

async function deleteConversation(id: string) {
  if (isProcessing.value) return
  // 先由服务端确认删除：运行中的会话会返回 409，本地不能抢先移除
  try {
    await deleteConversationApi(id)
  } catch (e) {
    handlePersistError('删除会话', e)
    return
  }
  delete conversations.value[id]
  const ids = Object.keys(conversations.value)
  if (ids.length === 0) {
    newConversation()
  } else if (currentId.value === id) {
    currentId.value = ids[0]
  }
}

async function sendMessage() {
  const text = input.value.trim()
  if (!text || isProcessing.value) return
  const convId = currentId.value
  const conv = conversations.value[convId]
  if (!conv) return

  // 本地先乐观展示；服务端在/stream 接口中原子写入用户消息，客户端不再单独提交
  conv.messages.push({ role: 'user', content: text })
  const userIdx = conv.messages.length - 1
  if (conv.messages.length === 1) {
    conv.title = text.length > 20 ? text.slice(0, 20) + '...' : text
    updateConversationTitle(convId, conv.title).catch(e => handlePersistError('更新标题', e))
  }
  input.value = ''
  resetInputHeight()
  thinkingStartTime = Date.now()
  thinkingPhase.value = ''
  isProcessing.value = true
  isThinking.value = true
  scrollToBottom()

  const assistantMsg: Message = { role: 'assistant', content: '', status: 'streaming' }
  conv.messages.push(assistantMsg)
  const idx = conv.messages.length - 1

  const captureThinkingTime = () => {
    if (conv.messages[idx].thinkingTime == null) {
      conv.messages[idx].thinkingTime = +((Date.now() - thinkingStartTime) / 1000).toFixed(1)
    }
  }

  // 上一次可能仍在途（正常情况下已被 isProcessing 挡住），先取消再开启新的
  streamAbort?.abort()
  const ac = new AbortController()
  streamAbort = ac
  // 幂等键：网络重试复用同一个值，服务端不会重复写入消息或重复生成
  const requestId = newRequestId()

  try {
    const outcome = await streamChat(
      convId, requestId, text, kbOnly.value,
      chunk => { if (isThinking.value) { captureThinkingTime(); isThinking.value = false } conv.messages[idx].content += chunk; scrollToBottom() },
      text => { if (isThinking.value) { captureThinkingTime(); isThinking.value = false } conv.messages[idx].content = text; scrollToBottom() },
      phase => { if (isThinking.value) thinkingPhase.value = PHASE_LABELS[phase] || '' },
      ac.signal,
    )
    captureThinkingTime()

    if (!outcome.started) {
      // 服务端没有接受本次请求（会话已删除、会话被占用或校验失败）：
      // 回滚本地乐观消息，避免展示数据库中不存在的问答
      conv.messages.splice(userIdx, 2)
      if (!ac.signal.aborted) {
        input.value = text
        resetInputHeight()
      }
      handlePersistError('发送消息', new Error(outcome.error || '请求未被服务端接受'))
      return
    }

    if (outcome.messageId != null) conv.messages[idx].id = outcome.messageId
    if (outcome.status === 'completed') {
      conv.messages[idx].status = 'completed'
      if (!conv.messages[idx].content) conv.messages[idx].content = '（未收到回复）'
    } else if (outcome.status === 'cancelled') {
      conv.messages[idx].status = 'cancelled'
    } else {
      conv.messages[idx].status = 'error'
      if (!conv.messages[idx].content) {
        conv.messages[idx].content = `错误: ${outcome.error || '生成失败'}`
      }
    }
    scrollToBottom()
  } catch (e) {
    captureThinkingTime()
    if (ac.signal.aborted) {
      conv.messages[idx].status = 'cancelled'
    } else {
      conv.messages[idx].status = 'error'
      if (!conv.messages[idx].content) conv.messages[idx].content = '网络错误，请检查连接后重试'
    }
    handlePersistError('发送消息', e)
    scrollToBottom()
  } finally {
    isProcessing.value = false
    isThinking.value = false
    thinkingPhase.value = ''
    scrollToBottom()
    if (streamAbort === ac) streamAbort = null
  }
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage() }
}

// ---------- knowledge state ----------
const files = ref<FileItem[]>([])
const knowledgeLoading = ref(false)
const knowledgeBuilding = ref(false)
const knowledgeUploading = ref(false)
const knowledgeMsg = ref('')
const knowledgeMsgType = ref<'success' | 'error' | ''>('')
const dragOver = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)
// 正在删除的文件名：非空时禁用删除按钮并在该行显示"删除中..."，避免重复点击
const deletingName = ref('')

// ---------- 操作失败提示 ----------
const persistNotice = ref('')
let persistNoticeTimer: number | null = null
function handlePersistError(op: string, e: unknown) {
  console.error(`[操作失败] ${op}:`, e)
  const detail = e instanceof Error && e.message ? e.message : ''
  persistNotice.value = detail ? `${op}失败：${detail}` : `${op}失败，请稍后重试`
  if (persistNoticeTimer !== null) clearTimeout(persistNoticeTimer)
  persistNoticeTimer = window.setTimeout(() => { persistNotice.value = '' }, 5000)
}

// ---------- index health state ----------
const health = ref<HealthReport | null>(null)
const healthChecking = ref(false)
const healthRepairing = ref(false)
const isAdmin = computed(() => user.value?.role === 'admin')
const healthIssueCount = ref(0)
let healthPollTimer: number | null = null

function updateHealthBadge() {
  healthIssueCount.value = health.value ? health.value.issues.length : 0
}

// 知识库操作互斥：检查/修复/补建/删除/上传/刷新同一时刻只允许一个。
// 后端这些操作共用一把索引锁：写与写之间排队等待上限 300 秒、超时 503，
// 只读自检等待写锁上限 30 秒；前端先互斥可避免请求排队、多个 spinner 同转与状态短时不一致
const knowledgeBusy = computed(() =>
  healthChecking.value || healthRepairing.value || knowledgeBuilding.value ||
  knowledgeUploading.value || knowledgeLoading.value || deletingName.value !== ''
)

// 自检报告摘要：只取与界面状态相关的字段（忽略 checked_at，它每次检查都会变），
// 用于判断自动轮询是否带回了实质变化（如后台定时自检/自愈修复了异常）
function healthFingerprint(report: HealthReport | null): string {
  if (!report) return ''
  return JSON.stringify({ healthy: report.healthy, issues: report.issues, stats: report.stats })
}

async function refreshHealthBadge() {
  if (!isAdmin.value) return
  try {
    const report = await fetchIndexHealth()
    healthIssueCount.value = report.issues.length
    // 在知识库页时，自动轮询发现报告有实质变化（后台自检/自愈的产物）：
    // 同步面板并刷新一次文档列表，保证「已入库」状态与自检结果判定标准一致；
    // 任意索引操作进行中则跳过，避免与手动流程互相覆盖
    if (mode.value === 'knowledge' && !knowledgeBusy.value
        && healthFingerprint(report) !== healthFingerprint(health.value)) {
      health.value = report
      await loadFiles()
    }
  } catch { /* 静默失败，保留原角标值 */ }
}

function startHealthPolling() {
  if (healthPollTimer !== null) { clearInterval(healthPollTimer); healthPollTimer = null }
  if (!isAdmin.value) return
  refreshHealthBadge()
  healthPollTimer = window.setInterval(refreshHealthBadge, 60_000)
}

function stopHealthPolling() {
  if (healthPollTimer !== null) { clearInterval(healthPollTimer); healthPollTimer = null }
  healthIssueCount.value = 0
}

async function loadFiles() {
  knowledgeLoading.value = true
  try { files.value = await fetchFiles() }
  catch (e) { showKnowledgeMsg((e as Error).message, 'error') }
  finally { knowledgeLoading.value = false }
}

function showKnowledgeMsg(text: string, type: 'success' | 'error') {
  knowledgeMsg.value = text
  knowledgeMsgType.value = type
  setTimeout(() => { knowledgeMsg.value = ''; knowledgeMsgType.value = '' }, 4000)
}

async function handleUpload(fileList: FileList | File[]) {
  const arr = Array.from(fileList)
  if (arr.length === 0) return
  knowledgeUploading.value = true
  try {
    const result = await uploadFiles(arr)
    const uploadedNames = result.uploaded.map(u => u.overwritten ? `${u.name}(覆盖)` : u.name)
    // 写入磁盘成功但入库失败的，与上传错误并列展示，管理员可当场得知未被检索的文件
    const indexFailures = result.uploaded
      .filter(u => !u.indexed)
      .map(u => `${u.name}: ${u.error || '入库失败'}`)
    const errorTexts = [...result.errors.map(e => `${e.name}: ${e.error}`), ...indexFailures]

    if (uploadedNames.length && !errorTexts.length) {
      showKnowledgeMsg(`已上传并入库: ${uploadedNames.join(', ')}`, 'success')
    } else if (errorTexts.length) {
      const prefix = uploadedNames.length ? `已上传: ${uploadedNames.join(', ')}。以下未成功：` : '失败：'
      showKnowledgeMsg(`${prefix}${errorTexts.join('；')}`, 'error')
    } else {
      showKnowledgeMsg('上传失败：没有有效文件', 'error')
    }
    await loadFiles()
  } catch (e) { showKnowledgeMsg((e as Error).message, 'error') }
  finally { knowledgeUploading.value = false }
}

function onFileSelect(e: Event) {
  const input = e.target as HTMLInputElement
  if (input.files) handleUpload(input.files)
  input.value = ''
}

function onDrop(e: DragEvent) {
  dragOver.value = false
  if (e.dataTransfer?.files) handleUpload(e.dataTransfer.files)
}

async function handleDeleteFile(name: string) {
  if (!confirm(`确定删除 ${name}？`)) return
  deletingName.value = name
  try { await deleteFile(name); showKnowledgeMsg(`已删除: ${name}`, 'success'); await loadFiles() }
  catch (e) { showKnowledgeMsg((e as Error).message, 'error') }
  finally { deletingName.value = '' }
}

async function handleBuild() {
  knowledgeBuilding.value = true
  try {
    const { message, failures } = await buildVector()
    if (failures && failures.length) {
      // 部分失败必须醒目提示：列出失败文件与原因（路径只显示文件名）
      const detail = failures
        .map(f => `${f.file.split(/[\\/]/).pop()}: ${f.error}`)
        .join('；')
      showKnowledgeMsg(`${message}（${detail}）`, 'error')
    } else {
      showKnowledgeMsg(message, 'success')
    }
    await loadFiles()
    await loadHealth(true)
  }
  catch (e) { showKnowledgeMsg((e as Error).message, 'error') }
  finally { knowledgeBuilding.value = false }
}

// ---------- index health actions ----------
async function loadHealth(refresh = false) {
  try { health.value = await fetchIndexHealth(refresh); updateHealthBadge() }
  catch (e) { showKnowledgeMsg((e as Error).message, 'error') }
}

async function handleHealthCheck() {
  healthChecking.value = true
  try {
    health.value = await fetchIndexHealth(true)
    updateHealthBadge()
    // 自检结果与文档列表的「已入库」状态同屏展示，检查完成后同步刷新一次列表，
    // 避免两处判定标准不一致（静默刷新，不额外提示）
    await loadFiles()
  }
  catch (e) { showKnowledgeMsg((e as Error).message, 'error') }
  finally { healthChecking.value = false }
}

async function handleHealthRepair() {
  if (!confirm('确认立即手动修复？系统定时自愈会自动处理，此处将马上清理残留向量、重建入库记录，并重新索引向量丢失的文件。')) return
  healthRepairing.value = true
  try {
    const report = await repairIndexHealth()
    health.value = report
    updateHealthBadge()
    const r = report.repair
    if (r) {
      const parts: string[] = []
      if (r.ghost_deleted.length) parts.push(`清理残留向量 ${r.ghost_deleted.length} 项`)
      if (r.ghost_errors.length) parts.push(`清理异常 ${r.ghost_errors.length} 项`)
      if (r.re_indexed.length) parts.push(`重索引 ${r.re_indexed.length} 个文件`)
      if (r.md5_rebuilt) parts.push('入库记录已重建')
      const hasErrors = r.ghost_errors.length > 0
      showKnowledgeMsg(
        parts.length ? `修复${hasErrors ? '部分失败' : '完成'}：${parts.join('；')}` : (hasErrors ? '修复失败' : '修复完成（无需变更）'),
        hasErrors ? 'error' : 'success'
      )
    } else {
      showKnowledgeMsg('修复完成', 'success')
    }
    await loadFiles()
  } catch (e) { showKnowledgeMsg((e as Error).message, 'error') }
  finally { healthRepairing.value = false }
}

function formatHealthTime(iso: string | null): string {
  if (!iso) return '从未'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString()
}

watch(mode, (newMode) => {
  if (newMode === 'knowledge') { loadFiles(); loadHealth() }
})

// ---------- auth actions ----------
async function handleLogin() {
  authError.value = ''
  authSuccess.value = ''
  if (!authUsername.value.trim() || !authPassword.value) {
    authError.value = '请填写用户名和密码'
    return
  }
  authLoading.value = true
  try {
    const u = await apiLogin(authUsername.value.trim(), authPassword.value)
    user.value = u
    initConversations()
    startHealthPolling()
  } catch (e) {
    authError.value = (e as Error).message
  } finally {
    authLoading.value = false
  }
}

async function handleRegister() {
  authError.value = ''
  if (!authUsername.value.trim() || !authPassword.value) {
    authError.value = '请填写用户名和密码'
    return
  }
  if (authPassword.value !== authPassword2.value) {
    authError.value = '两次输入的密码不一致'
    return
  }
  if (authPassword.value.length < 10 || !/\d/.test(authPassword.value) || !/[a-zA-Z]/.test(authPassword.value) || !/[^0-9a-zA-Z\s]/.test(authPassword.value)) {
    authError.value = '密码需至少10个字符，且包含数字、英文字母和特殊符号'
    return
  }
  authLoading.value = true
  try {
    await apiRegister(authUsername.value.trim(), authPassword.value)
    // 注册成功后切换到登录页
    authMode.value = 'login'
    authSuccess.value = '注册成功，请登录'
    authError.value = ''
    authPassword.value = ''
    authPassword2.value = ''
  } catch (e) {
    authError.value = (e as Error).message
  } finally {
    authLoading.value = false
  }
}

function handleLogout() {
  if (!confirm('确定要退出登录吗？')) return
  streamAbort?.abort()
  clearAuth()
  user.value = null
  mode.value = 'chat'
  showGearMenu.value = false
  conversations.value = {}
  currentId.value = ''
  authUsername.value = ''
  authPassword.value = ''
  authPassword2.value = ''
  authError.value = ''
  authSuccess.value = ''
  authMode.value = 'login'
  health.value = null
  stopHealthPolling()
}

// ---------- gear menu ----------
const showGearMenu = ref(false)

// 点击页面其他地方关闭齿轮菜单。抽成具名函数是为了卸载时能移除监听
function onDocumentClick(e: MouseEvent) {
  const target = e.target as HTMLElement
  if (!target.closest('.gear-wrapper')) {
    showGearMenu.value = false
  }
}

function toggleGearMenu() {
  showGearMenu.value = !showGearMenu.value
}

function goToKnowledge() {
  sidebarOpen.value = false
  mode.value = 'knowledge'
  localStorage.setItem(MODE_KEY, 'knowledge')
  showGearMenu.value = false
}

function goBackToChat() {
  mode.value = 'chat'
  localStorage.setItem(MODE_KEY, 'chat')
}

// 启动时验证 token 有效性
onMounted(async () => {
  if (user.value) {
    try {
      user.value = await apiMe()
      initConversations()
    }
    catch { user.value = null; clearAuth() }
  }
  // 刷新后恢复上次停留的页面；非管理员不能停留在知识库页
  if (mode.value === 'knowledge' && (!user.value || user.value.role !== 'admin')) {
    mode.value = 'chat'
    localStorage.setItem(MODE_KEY, 'chat')
  }
  // 刷新后直接停在知识库页时，watch(mode) 不会触发（mode 值没变化），需主动拉一次数据，
  // 否则文档列表与索引报告会空白，直到手动点「刷新」
  if (mode.value === 'knowledge') {
    loadFiles()
    loadHealth()
  }
  startHealthPolling()
  document.addEventListener('click', onDocumentClick)
})

// 组件卸载时断开在途流、停掉轮询，并移除文档级监听
onUnmounted(() => {
  streamAbort?.abort()
  stopHealthPolling()
  document.removeEventListener('click', onDocumentClick)
})
</script>

<template>
  <!-- ==================== 登录/注册页 ==================== -->
  <div v-if="!user" class="auth-page">
    <div class="auth-card">
      <svg class="auth-mark brand-mark" width="28" height="28" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M5 5h9a5 5 0 0 1 5 5v9h-9a5 5 0 0 1-5-5V5Z" stroke="currentColor" stroke-width="1.7"/><path d="M9 9h6M9 13h4" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>
      <h1 class="auth-title">浅问深答</h1>
      <p class="auth-subtitle">连接你的知识，找到可靠答案。</p>

      <div class="auth-tabs">
        <button :class="{ active: authMode === 'login' }" @click="authMode = 'login'; authError = ''; authSuccess = ''">登录</button>
        <button :class="{ active: authMode === 'register' }" @click="authMode = 'register'; authError = ''; authSuccess = ''">注册</button>
      </div>

      <div v-if="authSuccess" class="auth-success">{{ authSuccess }}</div>
      <div v-if="authError" class="auth-error">{{ authError }}</div>

      <form @submit.prevent="authMode === 'login' ? handleLogin() : handleRegister()" class="auth-form">
        <input
          v-model="authUsername"
          type="text"
          placeholder="用户名" aria-label="用户名"
          autocomplete="username"
          :disabled="authLoading"
        />
        <input
          v-model="authPassword"
          type="password"
          placeholder="密码" aria-label="密码"
          :autocomplete="authMode === 'login' ? 'current-password' : 'new-password'"
          :disabled="authLoading"
          @keydown.enter="authMode === 'login' ? handleLogin() : handleRegister()"
        />
        <input
          v-if="authMode === 'register'"
          v-model="authPassword2"
          type="password"
          placeholder="确认密码" aria-label="确认密码"
          autocomplete="new-password"
          :disabled="authLoading"
          @keydown.enter="handleRegister"
        />
        <button type="submit" class="btn-auth" :disabled="authLoading">
          {{ authLoading ? '处理中...' : (authMode === 'login' ? '登录' : '注册') }}
        </button>
      </form>
    </div>
  </div>

  <!-- ==================== 主界面 ==================== -->
  <div v-else class="layout" @keydown.esc="sidebarOpen = false">
    <button v-if="sidebarOpen" class="sidebar-backdrop" aria-label="关闭导航" @click="sidebarOpen = false"></button>
    <!-- 工作区导航 -->
    <aside class="sidebar" :class="{ open: sidebarOpen }" aria-label="工作区导航">
      <div class="sidebar-header">
        <div class="brand">
          <svg class="brand-mark" width="28" height="28" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M5 5h9a5 5 0 0 1 5 5v9h-9a5 5 0 0 1-5-5V5Z" stroke="currentColor" stroke-width="1.7"/><path d="M9 9h6M9 13h4" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>
          <span class="brand-name">浅问深答</span>
        </div>
        <button class="mobile-close" aria-label="关闭导航" @click="sidebarOpen = false">×</button>
        <button class="btn-new" :disabled="isProcessing || hasEmptyConv" @click="goBackToChat(); newConversation(); sidebarOpen = false" title="新建对话">
          <span class="plus">＋</span> 新建
        </button>
      </div>

      <nav class="workspace-nav">
        <button :class="{ selected: mode === 'chat' }" @click="goBackToChat(); sidebarOpen = false"><span aria-hidden="true">▤</span> 对话</button>
        <button v-if="user.role === 'admin'" :class="{ selected: mode === 'knowledge' }" :disabled="isProcessing" @click="goToKnowledge(); sidebarOpen = false"><span aria-hidden="true">▦</span> 知识库 <span class="nav-count" v-if="healthIssueCount">{{ healthIssueCount }}</span></button>
      </nav>
      <div class="sidebar-section-title">最近对话</div>

      <ul class="conv-list">
        <li v-for="c in convList" :key="c.id" :class="{ active: c.id === currentId }">
          <button class="conv-btn" :disabled="isProcessing" @click="goBackToChat(); selectConversation(c.id)">

            <span class="conv-title">{{ c.title }}</span>
          </button>
          <button class="del-btn" :disabled="isProcessing" @click="deleteConversation(c.id)" title="删除">×</button>
        </li>
      </ul>

      <!-- 左下角：用户标识 + 齿轮菜单 -->
      <div class="sidebar-footer">
        <div class="divider"></div>
        <div class="user-area">
          <div class="user-info">
            <span class="user-avatar">
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none"><path d="M12 12C14.7614 12 17 9.76142 17 7C17 4.23858 14.7614 2 12 2C9.23858 2 7 4.23858 7 7C7 9.76142 9.23858 12 12 12Z" fill="currentColor"/><path d="M12 14C7.58172 14 4 17.5817 4 22H20C20 17.5817 16.4183 14 12 14Z" fill="currentColor"/></svg>
            </span>
            <span class="user-name">{{ user.username }}</span>
            <span class="user-role" :class="user.role">{{ user.role === 'admin' ? '管理员' : '用户' }}</span>
          </div>
          <div class="gear-wrapper">
            <button class="btn-gear" @click="toggleGearMenu" title="设置">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.66a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
              <span v-if="healthIssueCount > 0" class="badge">{{ healthIssueCount }}</span>
            </button>
            <div v-if="showGearMenu" class="gear-menu">
              <button v-if="user.role === 'admin'" :disabled="isProcessing" @click="goToKnowledge">
                管理知识库
                <span v-if="healthIssueCount > 0" class="badge menu-badge">{{ healthIssueCount }}</span>
              </button>
              <button class="logout-item" @click="handleLogout">退出登录</button>
            </div>
          </div>
        </div>
      </div>
    </aside>

    <!-- 主区域 -->
    <main class="main">
      <header class="workspace-header">
        <button class="mobile-menu" aria-label="打开导航" :aria-expanded="sidebarOpen" @click="sidebarOpen = true">☰</button>
        <span class="header-context">工作区 <span class="header-slash">/</span></span>
        <span class="header-title">{{ mode === 'knowledge' ? '知识库' : (current?.title || '新对话') }}</span>
        <span class="header-mode">{{ mode === 'knowledge' ? '文档管理' : (kbOnly ? '知识库问答' : '通用问答') }}</span>
      </header>
      <!-- ===== 对话模式 ===== -->
      <template v-if="mode === 'chat'">
        <div ref="chatArea" class="chat-area">
          <template v-if="current">
            <div v-if="current.messages.length === 0" class="empty">
              <svg class="brand-mark empty-seal" width="28" height="28" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M5 5h9a5 5 0 0 1 5 5v9h-9a5 5 0 0 1-5-5V5Z" stroke="currentColor" stroke-width="1.7"/><path d="M9 9h6M9 13h4" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>
              <p class="empty-title">有什么想了解的？</p>
              <p class="empty-hint">从一个问题开始，让知识为你所用。</p>
              <div class="suggestions"><button v-for="(text, i) in suggestions" :key="text" @click="useSuggestion(text)"><span class="suggestion-index">0{{ i + 1 }}</span><span>{{ text }}</span><span aria-hidden="true">↗</span></button></div>
            </div>

            <div v-for="(msg, i) in current.messages" :key="i" class="message" :class="msg.role">
              <div class="avatar" :class="msg.role">
                <template v-if="msg.role === 'user'">
                  <svg class="brand-mark" width="28" height="28" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M5 5h9a5 5 0 0 1 5 5v9h-9a5 5 0 0 1-5-5V5Z" stroke="currentColor" stroke-width="1.7"/><path d="M9 9h6M9 13h4" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>
                </template>
                <template v-else>
                  <svg class="brand-mark" width="28" height="28" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M5 5h9a5 5 0 0 1 5 5v9h-9a5 5 0 0 1-5-5V5Z" stroke="currentColor" stroke-width="1.7"/><path d="M9 9h6M9 13h4" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>
                </template>
              </div>
              <div class="bubble-wrap">
                <div v-if="msg.role === 'assistant' && isThinking && i === current.messages.length - 1" class="thinking">
                  <span class="thinking-dot"></span>
                  <span class="thinking-dot" style="animation-delay:0.15s"></span>
                  <span class="thinking-dot" style="animation-delay:0.3s"></span>
                  <span class="thinking-text">{{ thinkingPhase || '处理中' }}...... {{ formatTime(thinkingSeconds) }}</span>
                </div>
                <div v-if="msg.role === 'assistant' && !(isThinking && !msg.content && i === current.messages.length - 1)" class="bubble markdown-body" v-html="renderMarkdown(bubbleText(msg))"></div>
                <div v-if="msg.role === 'assistant' && msg.thinkingTime != null && msg.content" class="thinking-time">· 处理耗时 {{ formatTime(msg.thinkingTime) }}</div>
                <div v-if="msg.role === 'assistant' && (msg.status === 'cancelled' || msg.status === 'error') && msg.content" class="message-note" :class="msg.status">
                  {{ msg.status === 'cancelled' ? '· 已取消' : '· 回答未完成' }}
                </div>
                <div v-if="msg.role === 'user'" class="bubble">{{ msg.content }}</div>
                <span v-if="isProcessing && !isThinking && i === current.messages.length - 1 && msg.role === 'assistant'" class="cursor">▌</span>
              </div>
            </div>
          </template>
        </div>

        <transition name="banner">
          <div v-if="persistNotice" class="persist-notice" @click="persistNotice = ''" title="点击关闭">
            {{ persistNotice }}
          </div>
        </transition>

        <div class="input-bar">
          <div class="input-inner">
            <div class="input-options">
              <div class="mode-switch" role="group" aria-label="问答模式">
                <button :class="{ active: kbOnly }" :aria-pressed="kbOnly" :disabled="isProcessing" @click="kbOnly = true">知识库问答</button>
                <button :class="{ active: !kbOnly }" :aria-pressed="!kbOnly" :disabled="isProcessing" @click="kbOnly = false">通用问答</button>
              </div>
              <span class="mode-description">{{ kbOnly ? '问候、身份询问、翻译改写等直接回答；其他问题检索内部资料' : '模型知识 · 按需联网' }}</span>
            </div>
            <div class="input-row">
              <textarea
                ref="inputEl"
                v-model="input" :placeholder="kbOnly ? '向知识库提问…' : '询问、探索，或开始一个想法…'" aria-label="输入问题" rows="2" :disabled="isProcessing"
                @keydown="onKeydown"
                @input="($event.target as HTMLTextAreaElement).style.height='auto';($event.target as HTMLTextAreaElement).style.height=Math.min(($event.target as HTMLTextAreaElement).scrollHeight,120)+'px'"
              />
              <button v-if="isProcessing" class="btn-send btn-stop" @click="stopStreaming" title="停止生成">
                <svg width="12" height="12" viewBox="0 0 24 24" aria-hidden="true"><rect x="5" y="5" width="14" height="14" rx="3" fill="currentColor"/></svg>
                停止
              </button>
              <button v-else class="btn-send" :disabled="!input.trim() || !current" @click="sendMessage" aria-label="发送消息" title="发送消息">↑</button>
            </div>
          </div>
          <p class="composer-hint">Enter 发送 <span>·</span> Shift + Enter 换行 <span>·</span> 重要信息请核对来源</p>
        </div>
      </template>

      <!-- ===== 文件管理模式 ===== -->
      <template v-if="mode === 'knowledge'">
        <div class="knowledge-page">
          <header class="knowledge-header">
            <button class="btn-back" @click="goBackToChat" title="返回对话">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 12H5M12 19l-7-7 7-7"/></svg>
            </button>
            <div>
              <h1>知识库管理</h1>
              <p class="subtitle">上传文档自动入库，立即可用于检索</p>
            </div>
          </header>

          <div v-if="knowledgeMsg" class="toast" :class="knowledgeMsgType">{{ knowledgeMsg }}</div>

          <div class="knowledge-cols">
            <div class="knowledge-left">
              <section class="card">
                <h2>上传文件</h2>
                <p class="hint">支持 .txt / .pdf / .docx，上传后自动入库并当场校验。</p>
                <div
                  class="dropzone" role="button" tabindex="0" aria-label="选择文件上传" @keydown.enter.prevent="fileInput?.click()" @keydown.space.prevent="fileInput?.click()" :class="{ over: dragOver, busy: knowledgeBusy }"
                  @dragover.prevent="dragOver = true"
                  @dragleave="dragOver = false"
                  @drop.prevent="onDrop"
                  @click="fileInput?.click()"
                >
                  <input ref="fileInput" type="file" accept=".txt,.pdf,.docx" multiple hidden @change="onFileSelect" />
                  <p v-if="knowledgeUploading">上传并入库中...</p>
                  <p v-else-if="knowledgeBusy">其他操作进行中，请稍候...</p>
                  <p v-else>点击或拖拽文件到此处</p>
                </div>
              </section>

              <section class="card">
                <div class="card-header">
                  <h2>索引健康</h2>
                  <div class="health-actions">
                    <button class="btn-ghost" :disabled="knowledgeBusy" @click="handleHealthCheck">
                      <span v-if="healthChecking" class="btn-spinner" aria-hidden="true"></span>
                      {{ healthChecking ? '检查中...' : '立即检查' }}
                    </button>
                    <button
                      class="btn-ghost" :disabled="knowledgeBusy" @click="handleHealthRepair"
                      title="系统定时自检发现问题会自动修复；此处可立即手动执行一次"
                    >
                      <span v-if="healthRepairing" class="btn-spinner" aria-hidden="true"></span>
                      {{ healthRepairing ? '修复中...' : '手动修复' }}
                    </button>
                  </div>
                </div>
                <p v-if="!health" class="hint">后台定时自检发现问题会自动修复；以下按钮用于手动检查或立即修复。</p>
                <template v-else>
                  <p class="health-summary">
                    <span class="health-dot" :class="health.healthy ? 'ok' : 'bad'"></span>
                    <strong>{{ health.healthy ? '状态良好' : `发现 ${health.issues.length} 类问题` }}</strong>
                    <span class="health-time">最近检查：{{ formatHealthTime(health.checked_at) }}</span>
                  </p>
                  <p class="hint">磁盘文件 {{ health.stats.files }} 个（已正确入库 {{ health.stats.indexed_files }} 个）· 向量块 {{ health.stats.chunks }}</p>
                  <ul v-if="health.issues.length" class="health-issues">
                    <li v-for="(iss, i) in health.issues" :key="i">
                      <span class="issue-sev" :class="iss.severity">{{ iss.severity === 'error' ? '严重' : '警告' }}</span>
                      <span class="issue-text">
                        {{ iss.title }}（{{ iss.count }}）
                        <em v-if="iss.detail" class="issue-detail">{{ iss.detail }}</em>
                      </span>
                    </li>
                  </ul>
                </template>
              </section>
            </div>

            <div class="knowledge-right">
              <section class="card">
                <div class="card-header">
                  <h2>文档列表</h2>
                  <div class="file-header-actions">
                    <button class="btn-ghost" :disabled="knowledgeBusy" @click="loadFiles">
                      <span v-if="knowledgeLoading" class="btn-spinner" aria-hidden="true"></span>
                      {{ knowledgeLoading ? '刷新中...' : '刷新' }}
                    </button>
                    <button
                      class="btn-ghost" :disabled="knowledgeBusy" @click="handleBuild"
                      title="重新扫描知识库目录，补建未入库文件"
                    >
                      <span v-if="knowledgeBuilding" class="btn-spinner" aria-hidden="true"></span>
                      {{ knowledgeBuilding ? '补建中...' : '补建向量库' }}
                    </button>
                  </div>
                </div>
                <div v-if="knowledgeLoading && files.length === 0" class="empty">加载中...</div>
                <div v-else-if="files.length === 0" class="empty">暂无文档</div>
                <ul v-else class="file-list">
                  <li v-for="f in files" :key="f.name">
                    <span class="file-name">{{ f.name }}</span>
                    <span class="file-actions">
                      <span class="file-status" :class="f.indexed ? 'indexed' : 'pending'">{{ f.indexed ? '已入库' : '未入库' }}</span>
                      <button class="btn-danger" :disabled="knowledgeBusy" @click="handleDeleteFile(f.name)">
                        {{ deletingName === f.name ? '删除中...' : '删除' }}
                      </button>
                    </span>
                  </li>
                </ul>
              </section>
            </div>
          </div>
        </div>
      </template>
    </main>
  </div>
</template>

<style scoped>
.layout { display: flex; height: 100vh; height: 100dvh; }
.brand-mark { flex-shrink: 0; color: var(--text); }
.sidebar { width: 256px; flex-shrink: 0; padding: 24px 12px 12px; background: var(--sidebar); display: flex; flex-direction: column; border-right: 1px solid var(--line); }
.sidebar-header { display: flex; align-items: center; gap: 8px; padding: 0 8px 24px; flex-wrap: wrap; }
.brand { display: flex; gap: 9px; align-items: center; flex: 1; white-space: nowrap; }
.brand-name { font-weight: 650; font-size: 16px; letter-spacing: -.3px; }
.btn-new { display: flex; align-items: center; gap: 9px; width: 100%; margin-top: 20px; padding: 10px 12px; background: var(--bg); border: 1px solid #dedede; border-radius: 9px; text-align: left; font-size: 13px; box-shadow: 0 1px 2px #00000004; }
.plus { font-size: 19px; line-height: 1; font-weight: 400; }
.btn-new:hover:not(:disabled) { background: var(--hover); }
.workspace-nav { display: grid; gap: 3px; margin-bottom: 26px; }
.workspace-nav button { display: flex; align-items: center; gap: 12px; padding: 10px 12px; border-radius: 7px; text-align: left; font-size: 13px; color: #626262; }
.workspace-nav button > span:first-child { font-size: 17px; }
.workspace-nav button.selected { color: var(--text); background: #eaeaec; font-weight: 550; }
.workspace-nav button:hover:not(:disabled) { background: var(--hover); }
.nav-count { margin-left: auto; }
.sidebar-section-title { padding: 0 12px 10px; font-size: 11px; color: var(--muted); font-weight: 550; }
.conv-list { list-style: none; margin: 0; padding: 0; flex: 1; overflow-y: auto; min-height: 0; }
.conv-list li { display: flex; align-items: center; border-radius: 7px; margin-bottom: 3px; }
.conv-list li:hover, .conv-list li.active { background: var(--hover); }
.conv-btn { flex: 1; min-width: 0; padding: 10px 12px; text-align: left; font-size: 13px; }
.conv-title { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.del-btn { width: 30px; height: 30px; flex-shrink: 0; opacity: 0; font-size: 19px; color: var(--muted); border-radius: 5px; }
.conv-list li:hover .del-btn, .conv-list li:focus-within .del-btn { opacity: 1; }
.del-btn:hover { color: var(--danger); background: #fff; }
.sidebar-footer { margin-top: auto; padding: 12px 5px 0; }
.divider { height: 1px; background: var(--line); margin-bottom: 14px; }
.user-area, .user-info { display: flex; align-items: center; gap: 9px; min-width: 0; }
.user-info { flex: 1; }
.user-avatar { width: 30px; height: 30px; background: #e5e5e7; color: #646466; border-radius: 50%; display: grid; place-items: center; flex-shrink: 0; }
.user-name { font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.user-role { font-size: 10px; color: var(--muted); white-space: nowrap; }
.gear-wrapper { position: relative; }
.btn-gear { width: 32px; height: 32px; display: grid; place-items: center; border-radius: 7px; position: relative; color: var(--muted); }
.btn-gear:hover { background: var(--hover); }
.badge { background: var(--danger); color: white; border-radius: 20px; font-size: 10px; padding: 1px 4px; position: absolute; top: -3px; right: -3px; }
.menu-badge { position: static; margin-left: 8px; }
.gear-menu { position: absolute; bottom: 42px; right: 0; width: 180px; background: white; border: 1px solid var(--line); border-radius: 10px; padding: 5px; box-shadow: 0 8px 28px #00000014; z-index: 30; }
.gear-menu button { display: block; padding: 10px; font-size: 13px; width: 100%; text-align: left; border-radius: 5px; }
.gear-menu button:hover { background: var(--surface); }
.logout-item { color: var(--danger); }
.main { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.workspace-header { display: flex; align-items: center; height: 64px; flex-shrink: 0; padding: 0 28px; gap: 12px; border-bottom: 1px solid #f0f0f0; font-size: 12px; }
.header-context { color: #8a8a8a; white-space: nowrap; }
.header-slash { color: #ccc; margin-left: 12px; }
.header-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.header-mode { margin-left: auto; padding: 5px 9px; border: 1px solid var(--line); border-radius: 6px; font-size: 11px; color: var(--muted); white-space: nowrap; }
.mobile-menu, .mobile-close, .sidebar-backdrop { display: none; }
.chat-area { flex: 1; min-height: 0; overflow-y: auto; padding: 36px 32px 24px; }
.empty { min-height: 100%; display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center; padding: 24px 0; }
.empty-seal { width: 42px; height: 42px; margin-bottom: 20px; }
.empty-title { font-size: clamp(25px, 3vw, 32px); letter-spacing: -1px; font-weight: 600; margin: 0 0 12px; }
.empty-hint { font-size: 14px; color: var(--muted); margin: 0; }
.suggestions { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; width: 100%; max-width: 680px; margin-top: 36px; }
.suggestions button { border: 1px solid var(--line); border-radius: 10px; text-align: left; padding: 15px; display: flex; flex-wrap: wrap; align-items: center; gap: 13px; font-size: 12px; line-height: 1.6; transition: background .15s, border-color .15s; }
.suggestions button:hover { background: var(--surface); border-color: #ccc; }
.suggestion-index { width: 100%; color: #969696; font-family: var(--font-mono); font-size: 11px; }
.suggestions button > span:nth-child(2) { flex: 1; }
.suggestions button > span:last-child { color: #949494; }
.message { display: flex; gap: 12px; max-width: 768px; margin: 0 auto 36px; }
.avatar { width: 28px; height: 28px; display: grid; place-items: center; flex-shrink: 0; margin-top: 4px; }
.avatar .brand-mark { width: 22px; height: 22px; }
.message.user { justify-content: flex-end; }
.message.user .avatar { display: none; }
.bubble-wrap { min-width: 0; flex: 1; }
.message.user .bubble-wrap { flex: 0 1 auto; max-width: 85%; }
.bubble { font-size: 15px; line-height: 1.85; overflow-wrap: anywhere; }
.message.user .bubble { background: #f3f3f3; border-radius: 16px; padding: 10px 18px; white-space: pre-wrap; }
.thinking { display: flex; gap: 5px; align-items: center; min-height: 32px; }
.thinking-dot { width: 4px; height: 4px; background: #777; border-radius: 50%; animation: pulse 1.3s infinite; }
.thinking-text { color: var(--muted); font-size: 12px; margin-left: 6px; }
.thinking-time { font-size: 11px; color: #898989; margin-top: 10px; }
.message-note { font-size: 11px; color: #898989; margin-top: 10px; }
.message-note.error { color: var(--danger); }
.cursor { animation: pulse 1s infinite; }
.input-bar { padding: 12px 32px 16px; }
.input-inner { display: flex; flex-direction: column; max-width: 768px; margin: 0 auto; border: 1px solid #dedede; border-radius: 17px; padding: 14px 16px 12px; box-shadow: 0 4px 20px #00000005; background: white; }
.input-inner:focus-within { border-color: #aaa; box-shadow: 0 4px 20px #00000008; }
.input-row { order: 0; display: flex; gap: 12px; align-items: flex-end; }
.input-row textarea { width: 100%; resize: none; border: 0; background: transparent; padding: 4px 0; font-size: 14px; line-height: 1.7; min-height: 60px; max-height: 160px; }
.input-row textarea:focus-visible { outline: none; }
.btn-send { width: 34px; height: 34px; border-radius: 50%; background: var(--accent); color: white; flex-shrink: 0; display: grid; place-items: center; font-size: 22px; }
.btn-send:hover:not(:disabled) { background: #444; }
.btn-stop { width: auto; border-radius: 8px; padding: 0 12px; display: flex; gap: 6px; font-size: 12px; }
.input-options { order: 1; display: flex; align-items: center; gap: 12px; margin-top: 12px; }
.mode-switch { display: flex; gap: 3px; background: #f4f4f4; padding: 3px; border-radius: 7px; }
.mode-switch button { font-size: 11px; color: var(--muted); padding: 5px 9px; border-radius: 5px; }
.mode-switch button.active { background: white; color: var(--text); box-shadow: 0 1px 3px #00000012; }
.mode-description { font-size: 11px; color: var(--muted); }
.composer-hint { font-size: 10px; color: #858585; text-align: center; margin: 11px 0 0; }
.composer-hint span { margin: 0 6px; color: #b5b5b5; }
.persist-notice { margin: 0 auto; padding: 8px 16px; color: var(--danger); background: #fff4f2; font-size: 12px; border-radius: 8px; max-width: 90%; cursor: pointer; }
.auth-page { display: grid; place-items: center; height: 100vh; height: 100dvh; overflow-y: auto; padding: 32px 24px; background: #fafafa; }
.auth-card { width: 100%; max-width: 390px; padding: 36px; border: 1px solid var(--line); border-radius: 18px; background: white; box-shadow: 0 12px 50px #00000004; }
.auth-mark { display: block; width: 36px; height: 36px; margin: 0 auto 20px; }
.auth-title { font-size: 26px; font-weight: 600; text-align: center; margin: 0; letter-spacing: -.6px; }
.auth-subtitle { font-size: 13px; color: var(--muted); text-align: center; margin: 12px 0 30px; }
.auth-tabs { display: flex; padding: 4px; background: var(--surface); border-radius: 8px; margin-bottom: 22px; }
.auth-tabs button { flex: 1; padding: 8px; font-size: 13px; color: var(--muted); border-radius: 5px; }
.auth-tabs button.active { color: var(--text); background: white; box-shadow: 0 1px 3px #0000000c; }
.auth-form { display: flex; flex-direction: column; gap: 12px; }
.auth-form input { width: 100%; padding: 12px; border: 1px solid #ddd; border-radius: 8px; font-size: 14px; background: white; }
.btn-auth { margin-top: 6px; background: var(--accent); color: white; border-radius: 8px; padding: 12px; font-size: 14px; }
.btn-auth:hover:not(:disabled) { background: #444; }
.auth-error, .auth-success { padding: 10px 12px; font-size: 12px; border-radius: 7px; margin-bottom: 16px; line-height: 1.6; }
.auth-error { color: var(--danger); background: #fff1ef; }
.auth-success { color: var(--success); background: #edf8f1; }
.knowledge-page { flex: 1; overflow-y: auto; padding: 36px; }
.knowledge-header { display: flex; gap: 16px; align-items: center; margin-bottom: 32px; }
.knowledge-header h1 { font-size: 26px; font-weight: 600; letter-spacing: -.7px; margin: 0; }
.subtitle { font-size: 13px; color: var(--muted); margin: 9px 0 0; }
.btn-back { display: grid; place-items: center; width: 34px; height: 34px; border: 1px solid var(--line); border-radius: 8px; }
.btn-back:hover { background: var(--surface); }
.knowledge-cols { display: grid; grid-template-columns: minmax(260px, .8fr) minmax(320px, 1.2fr); gap: 24px; max-width: 1280px; }
.knowledge-left, .knowledge-right { min-width: 0; }
.card { border: 1px solid var(--line); border-radius: 12px; padding: 22px; margin-bottom: 20px; background: white; }
.card h2 { font-size: 15px; font-weight: 600; margin: 0; }
.card-header { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; justify-content: space-between; margin-bottom: 18px; }
.hint { font-size: 12px; line-height: 1.7; color: var(--muted); margin: 12px 0 16px; }
.dropzone { border: 1px dashed #cdcdcd; border-radius: 9px; background: #fafafa; min-height: 150px; padding: 28px 16px; display: grid; place-items: center; text-align: center; font-size: 13px; color: #666; cursor: pointer; }
.dropzone::before { content: '↑'; font-size: 28px; color: #555; }
.dropzone p { margin: 10px 0 0; }
.dropzone.over, .dropzone:hover { border-color: #777; background: #f1f1f1; }
.dropzone.busy { opacity: .5; cursor: wait; }
.health-actions, .file-header-actions, .file-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.btn-ghost, .btn-danger { display: inline-flex; align-items: center; gap: 6px; border: 1px solid var(--line); border-radius: 6px; padding: 6px 9px; font-size: 11px; white-space: nowrap; }
.btn-ghost:hover:not(:disabled) { background: var(--surface); border-color: #ccc; }
.btn-danger { color: var(--danger); border-color: transparent; }
.btn-danger:hover:not(:disabled) { background: #fff1ef; }
.btn-spinner { width: 11px; height: 11px; border: 1px solid #ccc; border-top-color: #555; border-radius: 50%; animation: spin .8s linear infinite; }
.file-list { list-style: none; padding: 0; margin: 0; }
.file-list li { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 14px 0; border-bottom: 1px solid var(--line); }
.file-list li:last-child { border-bottom: 0; }
.file-name { font-size: 13px; overflow-wrap: anywhere; }
.file-actions { flex-shrink: 0; }
.file-status { font-size: 10px; padding: 4px 7px; border-radius: 5px; }
.file-status.indexed { color: var(--success); background: #edf8f1; }
.file-status.pending { color: #986918; background: #fff7e5; }
.card > .empty { min-height: 180px; font-size: 13px; color: var(--muted); }
.health-summary { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; font-size: 13px; }
.health-time { width: 100%; color: var(--muted); font-size: 11px; margin-top: 5px; }
.health-dot { width: 7px; height: 7px; border-radius: 50%; }
.health-dot.ok { background: var(--success); }
.health-dot.bad { background: var(--danger); }
.health-issues { list-style: none; margin: 0; padding: 0; }
.health-issues li { display: flex; align-items: baseline; gap: 8px; border-top: 1px solid var(--line); padding: 12px 0; font-size: 12px; }
.issue-sev { font-size: 10px; padding: 2px 5px; background: #fff7e5; color: #986918; flex-shrink: 0; border-radius: 4px; }
.issue-sev.error { background: #fff1ef; color: var(--danger); }
.issue-detail { display: block; font-style: normal; color: var(--muted); line-height: 1.7; margin-top: 5px; overflow-wrap: anywhere; }
.toast { padding: 12px 16px; margin-bottom: 20px; border-radius: 8px; background: #edf8f1; color: var(--success); font-size: 13px; }
.toast.error { background: #fff1ef; color: var(--danger); }
.markdown-body :deep(p) { margin: 0 0 14px; }
.markdown-body :deep(p:last-child) { margin-bottom: 0; }
.markdown-body :deep(h1), .markdown-body :deep(h2), .markdown-body :deep(h3), .markdown-body :deep(h4) { line-height: 1.5; font-weight: 650; margin: 24px 0 12px; letter-spacing: -.3px; }
.markdown-body :deep(h1) { font-size: 23px; }
.markdown-body :deep(h2) { font-size: 20px; }
.markdown-body :deep(h3) { font-size: 17px; }
.markdown-body :deep(h4) { font-size: 15px; }
.markdown-body :deep(ul), .markdown-body :deep(ol) { padding-left: 24px; margin: 12px 0; }
.markdown-body :deep(li) { margin: 5px 0; }
.markdown-body :deep(a) { color: #365e93; text-decoration: underline; text-underline-offset: 3px; }
.markdown-body :deep(blockquote) { border-left: 3px solid #ddd; padding-left: 16px; margin: 18px 0; color: #666; }
.markdown-body :deep(code) { font-family: var(--font-mono); font-size: .86em; background: #f1f1f1; padding: 3px 5px; border-radius: 4px; }
.markdown-body :deep(pre) { background: #f7f7f8; border: 1px solid var(--line); border-radius: 10px; padding: 16px; overflow-x: auto; line-height: 1.65; }
.markdown-body :deep(pre code) { background: none; padding: 0; }
.markdown-body :deep(table) { display: block; overflow-x: auto; border-collapse: collapse; margin: 18px 0; font-size: 13px; }
.markdown-body :deep(th), .markdown-body :deep(td) { border: 1px solid var(--line); padding: 9px 12px; text-align: left; }
.markdown-body :deep(th) { background: #f7f7f7; font-weight: 600; }
.markdown-body :deep(hr) { border: 0; border-top: 1px solid var(--line); margin: 24px 0; }
.markdown-body :deep(img) { max-width: 100%; border-radius: 8px; }
@keyframes pulse { 50% { opacity: .25; } }
@keyframes spin { to { transform: rotate(360deg); } }
@media (max-width: 1100px) { .knowledge-cols { grid-template-columns: 1fr; } .knowledge-left { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; } }
@media (max-width: 760px) {
  .sidebar { position: fixed; inset: 0 auto 0 0; width: 272px; z-index: 50; visibility: hidden; transform: translateX(-100%); transition: transform .2s, visibility .2s; }
  .sidebar.open { visibility: visible; transform: translateX(0); }
  .sidebar-backdrop { display: block; position: fixed; inset: 0; background: #0005; z-index: 45; }
  .mobile-close, .mobile-menu { display: grid; place-items: center; width: 30px; height: 30px; border-radius: 6px; font-size: 19px; flex-shrink: 0; }
  .mobile-close:hover, .mobile-menu:hover { background: var(--hover); }
  .workspace-header { height: 56px; padding: 0 16px; gap: 8px; }
  .header-context { display: none; }
  .header-mode { font-size: 10px; }
  .chat-area { padding: 24px 18px; }
  .empty-title { font-size: 26px; }
  .suggestions { grid-template-columns: 1fr; max-width: 360px; gap: 8px; margin-top: 26px; }
  .suggestions button { padding: 11px 14px; }
  .suggestion-index { width: auto; }
  .input-bar { padding: 8px 12px max(12px, env(safe-area-inset-bottom)); }
  .input-inner { padding: 12px; }
  .mode-description { display: none; }
  .message { gap: 8px; margin-bottom: 28px; }
  .message.user .bubble-wrap { max-width: 92%; }
  .bubble { font-size: 14px; }
  .knowledge-page { padding: 24px 16px; }
  .knowledge-left { display: block; }
  .knowledge-cols { display: block; }
  .card { padding: 18px; }
  .file-list li { align-items: flex-start; flex-direction: column; }
  .del-btn { opacity: 1; }
  .auth-card { padding: 28px 24px; }
}
@media (max-height: 650px) { .empty-seal { display: none; } .empty { padding: 8px 0; } .suggestions { margin-top: 20px; } }
</style>
