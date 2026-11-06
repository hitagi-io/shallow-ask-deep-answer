import { ref, reactive, computed, watch, nextTick, onMounted, onUnmounted, type Ref } from 'vue'
import { fetchConversations, fetchMessages, createConversationApi, updateConversationTitle, deleteConversationApi, streamChat, cancelGeneration, getStoredUser, newRequestId, withEmptyHistory, type Conversation, type Message, type Project } from '../api'
import { useNotice } from './useNotice'
import type { ConfirmAction } from './useConfirmation'

const PROJECT_REQUIRED_NOTICE = '请先选择一个可用项目，再发送知识库问题'

export function useChat(projects: Ref<Project[]>, confirmAction: ConfirmAction = async () => false) {
  let disposed = false
  // 输入框随内容自适应高度；清空内容时需手动复位（程序化清空不会触发 input 事件）
  const inputEl = ref<HTMLTextAreaElement | null>(null)
  function resetInputHeight() {
    nextTick(() => {
      if (inputEl.value) inputEl.value.style.height = 'auto'
    })
  }

  const { message: persistNotice, show: showNotice, clear: clearNotice } = useNotice(5000)
  function handlePersistError(op: string, e: unknown) {
    if (disposed) return
    console.error(`[操作失败] ${op}:`, e)
    const detail = e instanceof Error ? e.message : ''
    showNotice(detail ? `${op}失败：${detail}` : `${op}失败，请稍后重试`, 'error')
  }
  const conversations = ref<Record<string, Conversation>>({})
  const currentId = ref('')
  const selectionKey = `shallow_ask_deep_answer_conversation_${getStoredUser()?.id ?? ''}`
  const drafts = ref<Record<string, string>>({})
  const input = computed({ get: () => drafts.value[currentId.value] || '', set: value => { drafts.value[currentId.value] = value } })
  const loading = ref(false)
  const loadError = ref('')
  const nextCursor = ref<string | null>(null)
  const loadingMore = ref(false)
  const conversationBusy = ref(false)
  const failedAttempts = ref<Record<string, { text: string; mode: boolean; projectId: string | null; requestId: string }>>({})
  const canRetrySend = computed(() => {
    const attempt = failedAttempts.value[currentId.value]
    return !!attempt && attempt.mode === kbOnly.value && attempt.projectId === projectId.value && projectReady.value
  })
  const following = ref(true)
  const visibleCount = ref(50)
  interface GenerationRun {
    requestId: string
    startedAt: number
    thinking: boolean
    phase: string
    reconnecting: boolean
    stopping: boolean
    confirmed: boolean
    cancelSent: boolean
  }
  const runs = ref<Record<string, GenerationRun>>({})
  const controllers = new Map<string, AbortController>()
  const activeRun = computed(() => runs.value[currentId.value])
  const isProcessing = computed(() => !!activeRun.value)
  const processingIds = computed(() => Object.keys(runs.value))
  const isThinking = computed(() => !!activeRun.value?.thinking)
  const now = ref(Date.now())
  const thinkingSeconds = computed(() => activeRun.value ? Math.max(0, (now.value - activeRun.value.startedAt) / 1000) : 0)
  const thinkingPhase = computed(() => activeRun.value?.stopping ? '正在停止回答' : activeRun.value?.reconnecting ? '连接中断，正在重新连接' : activeRun.value?.phase || '')
  const reconnecting = computed(() => !!activeRun.value?.reconnecting)
  const stopping = computed(() => !!activeRun.value?.stopping)
  const PHASE_LABELS: Record<string, string> = {
    thinking: '正在思考',
    retrieving: '正在查找知识库资料',
    searching: '正在联网搜索',
    working: '正在处理问题',
    generating: '正在整理回答',
  }
  let thinkingTimer: number | null = null
  const chatArea = ref<HTMLElement | null>(null)
  const kbOnly = computed(() => conversations.value[currentId.value]?.kbOnly ?? true)
  const projectId = computed(() => conversations.value[currentId.value]?.project_id || null)
  const selectedProject = computed(() => projects.value.find(project => project.id === projectId.value))
  const projectReady = computed(() => !kbOnly.value || !!selectedProject.value)
  watch(projectReady, ready => {
    if (ready && persistNotice.value === PROJECT_REQUIRED_NOTICE) clearNotice()
  })
  const isEmptyConversation = (conv: Conversation) => !conv.hasMessages && conv.messages.length === 0
  async function initConversations() {
    if (loading.value) return
    loading.value = true
    loadError.value = ''
    try {
      const page = await fetchConversations()
      if (disposed) return
      nextCursor.value = page.next_cursor
      const loaded: Record<string, Conversation> = {}
      for (const c of page.conversations) loaded[c.id] = withEmptyHistory(c)
      if (Object.keys(loaded).length === 0) {
        const conv = await createConversationApi('新对话')
        loaded[conv.id] = conv
      }
      if (disposed) return
      conversations.value = loaded
      const selected = localStorage.getItem(selectionKey) || ''
      currentId.value = loaded[selected] ? selected : Object.keys(loaded)[0] || ''
    } catch (e) {
      loadError.value = e instanceof Error ? e.message : '会话加载失败'
    } finally { loading.value = false }
  }

  async function loadMoreConversations() {
    if (loadingMore.value || !nextCursor.value) return
    loadingMore.value = true
    try {
      const page = await fetchConversations(nextCursor.value)
      if (disposed) return
      for (const c of page.conversations) {
        if (!conversations.value[c.id]) conversations.value[c.id] = withEmptyHistory(c)
      }
      nextCursor.value = page.next_cursor
    } catch (e) { handlePersistError('加载更多会话', e) }
    finally { loadingMore.value = false }
  }

  async function loadMessages(id = currentId.value, earlier = false) {
    const conv = conversations.value[id]
    if (!conv) return
    if (conv.messagesLoading || (!earlier && runs.value[id]) || (earlier && !conv.nextBefore)) return
    conv.messagesLoading = true
    conv.messagesError = ''
    try {
      const page = await fetchMessages(id, earlier ? conv.nextBefore : null)
      if (disposed || conversations.value[id] !== conv) return
      const knownIds = new Set(conv.messages.map(m => m.id))
      conv.messages = earlier
        ? [...page.messages.filter(m => !knownIds.has(m.id)), ...conv.messages]
        : page.messages
      conv.nextBefore = page.next_before
      conv.messagesLoaded = true
      if (page.messages.length) conv.hasMessages = true
      if (page.kbOnly != null) conv.kbOnly = page.kbOnly
      if (page.kbOnly != null) {
        conv.project_id = page.project_id
        conv.project_name = page.project_name
      }
      if (page.active_generation && !runs.value[id]) {
        void observeGeneration(id, page.active_generation.request_id)
      }
      if (!earlier && currentId.value === id) jumpToLatest()
    } catch (e) {
      conv.messagesError = e instanceof Error ? e.message : '消息加载失败'
    } finally { conv.messagesLoading = false }
  }

  watch(isProcessing, (processing) => {
    if (thinkingTimer != null) { clearInterval(thinkingTimer); thinkingTimer = null }
    now.value = Date.now()
    if (processing) thinkingTimer = window.setInterval(() => { now.value = Date.now() }, 100)
  })

  const current = computed(() => conversations.value[currentId.value])
  const convList = computed(() => Object.values(conversations.value).sort((a, b) => new Date(b.updatedAt || 0).getTime() - new Date(a.updatedAt || 0).getTime()))
  const hasEmptyConv = computed(() => convList.value.some(isEmptyConversation))
  const messageOffset = computed(() => Math.max(0, (current.value?.messages.length || 0) - visibleCount.value))
  const visibleMessages = computed(() => current.value?.messages.slice(messageOffset.value) || [])
  const hasEarlier = computed(() => messageOffset.value > 0 || !!current.value?.nextBefore)
  async function showEarlier() {
    const conv = current.value
    if (!conv || conv.messagesLoading) return
    const id = currentId.value
    const mode = kbOnly.value
    const el = chatArea.value
    const height = el?.scrollHeight || 0
    const top = el?.scrollTop || 0
    following.value = false
    if (messageOffset.value > 0) visibleCount.value += 50
    else {
      const count = conv.messages.length
      await loadMessages(id, true)
      if (currentId.value !== id || kbOnly.value !== mode) return
      visibleCount.value += (current.value?.messages.length || 0) - count
    }
    await nextTick()
    if (el && currentId.value === id && kbOnly.value === mode) el.scrollTop = top + el.scrollHeight - height
  }
  function onScroll() {
    const el = chatArea.value
    if (el) following.value = el.scrollHeight - el.scrollTop - el.clientHeight < 80
  }
  function jumpToLatest() { following.value = true; scrollToBottom() }
  watch([currentId, kbOnly], ([id]) => {
    visibleCount.value = 50
    if (id) localStorage.setItem(selectionKey, id)
    if (conversations.value[id]) void loadMessages(id)
    resetInputHeight()
    jumpToLatest()
  })

  function scrollToBottom() {
    nextTick(() => {
      if (following.value && chatArea.value) chatArea.value.scrollTop = chatArea.value.scrollHeight
    })
  }

  async function newConversation(mode = kbOnly.value, selectedProjectId: string | null = null) {
    if (conversationBusy.value || loading.value) return
    const active = conversations.value[currentId.value]
    const empty = active && isEmptyConversation(active) ? active : convList.value.find(isEmptyConversation)
    if (empty) {
      empty.kbOnly = mode
      empty.project_id = mode ? selectedProjectId : null
      empty.project_name = projects.value.find(project => project.id === empty.project_id)?.name || null
      delete failedAttempts.value[empty.id]
      currentId.value = empty.id
      return
    }
    conversationBusy.value = true
    try {
      // 服务端也会复用空白会话，涵盖尚未加载到的分页和并发新建。
      const conv = await createConversationApi('新对话')
      if (disposed) return
      // 首次发送尚在登记时，服务端可能仍把它识别为空白会话。
      // 保留已有对象，避免覆盖运行中的订阅及乐观消息。
      if (runs.value[conv.id] || conversations.value[conv.id]?.hasMessages) {
        showNotice('问题正在提交，请稍后再开启新对话', 'error')
        return
      }
      conv.kbOnly = mode
      conv.project_id = mode ? selectedProjectId : null
      conv.project_name = projects.value.find(project => project.id === conv.project_id)?.name || null
      conversations.value[conv.id] = conv
      currentId.value = conv.id
    } catch (e) {
      handlePersistError('创建会话', e)
    } finally { conversationBusy.value = false }
  }

  async function switchMode(mode: boolean) {
    if (mode === kbOnly.value) return
    await newConversation(mode)
  }

  async function selectProject(id: string | null) {
    if (!kbOnly.value || id === projectId.value || (id && !projects.value.some(project => project.id === id))) return
    await newConversation(true, id)
  }

  function selectConversation(id: string) {
    if (conversationBusy.value || loading.value || !conversations.value[id]) return
    currentId.value = id
  }

  async function deleteConversation(id: string) {
    if (runs.value[id] || conversationBusy.value || !await confirmAction('删除此对话及全部消息？此操作无法撤销。')) return
    if (runs.value[id] || conversationBusy.value) return
    conversationBusy.value = true
    // 先由服务端确认删除：运行中的会话会返回 409，本地不能抢先移除
    try {
      await deleteConversationApi(id)
    } catch (e) {
      handlePersistError('删除会话', e)
      return
    } finally { conversationBusy.value = false }
    delete conversations.value[id]
    delete drafts.value[id]
    delete failedAttempts.value[id]
    if (Object.keys(conversations.value).length === 0 && nextCursor.value) await loadMoreConversations()
    const ids = Object.keys(conversations.value)
    if (ids.length === 0) {
      newConversation()
    } else if (currentId.value === id) {
      currentId.value = ids[0]
    }
  }

  async function renameConversation(id: string, title: string) {
    title = title.trim()
    if (!title || title.length > 255 || conversationBusy.value) return false
    conversationBusy.value = true
    try {
      await updateConversationTitle(id, title)
      conversations.value[id].title = title
      conversations.value[id].updatedAt = new Date().toISOString()
      return true
    } catch (e) { handlePersistError('重命名', e); return false }
    finally { conversationBusy.value = false }
  }
  async function removeProjectConversations(id: string) {
    for (const conv of Object.values(conversations.value)) {
      if (conv.project_id !== id) continue
      controllers.get(conv.id)?.abort()
      delete conversations.value[conv.id]
      delete drafts.value[conv.id]
      delete failedAttempts.value[conv.id]
    }
    if (!conversations.value[currentId.value]) {
      currentId.value = convList.value[0]?.id || ''
      if (!currentId.value) await newConversation()
    }
  }
  function retryMessage(index: number) {
    const question = current.value?.messages.slice(0, index).reverse().find(m => m.role === 'user')
    if (question && !isProcessing.value) void sendMessage(question.content, current.value?.messages[index].kbOnly ?? kbOnly.value)
  }
  function retryFailedSend() {
    const attempt = failedAttempts.value[currentId.value]
    if (attempt && canRetrySend.value) return sendMessage(attempt.text, attempt.mode, attempt.requestId)
  }

  async function requestStop(id: string, run: GenerationRun) {
    if (!run.confirmed || run.cancelSent) return
    run.cancelSent = true
    try {
      await cancelGeneration(id, run.requestId)
    } catch (e) {
      run.stopping = false
      run.cancelSent = false
      handlePersistError('停止回答', e)
    }
  }

  function stopStreaming() {
    const id = currentId.value
    const run = runs.value[id]
    if (!run || run.stopping) return
    run.stopping = true
    void requestStop(id, run)
  }

  async function observeGeneration(id: string, requestId: string, submission?: {
    text: string; mode: boolean; projectId: string | null; user: Message; assistant: Message; hadMessages: boolean
  }) {
    const conv = conversations.value[id]
    if (!conv || runs.value[id] || disposed) return
    runs.value[id] = { requestId, startedAt: Date.now(), thinking: true, phase: '', reconnecting: false, stopping: false, confirmed: false, cancelSent: false }
    const run = runs.value[id]
    const ac = new AbortController()
    controllers.set(id, ac)
    try {
      const outcome = await streamChat(id, requestId, snapshot => {
        if (disposed) return
        run.confirmed = true
        run.startedAt = Date.now() - snapshot.elapsed_seconds * 1000
        now.value = Date.now()
        run.phase = PHASE_LABELS[snapshot.phase] || ''
        let assistant = conv.messages.find(m => m.id === snapshot.assistant_message_id)
        if (assistant && submission && assistant !== submission.assistant) {
          conv.messages = conv.messages.filter(m => m !== submission.assistant)
        }
        if (!assistant) {
          assistant = submission?.assistant || reactive<Message>({ role: 'assistant', content: '' })
          if (!conv.messages.includes(assistant)) conv.messages.push(assistant)
        }
        assistant.id = snapshot.assistant_message_id
        assistant.content = snapshot.assistant_content || ''
        assistant.kbOnly = snapshot.kb_only
        conv.kbOnly = snapshot.kb_only
        conv.project_id = snapshot.project_id
        conv.project_name = snapshot.project_name
        assistant.status = snapshot.status === 'running' ? 'streaming' : snapshot.status === 'failed' ? 'error' : snapshot.status
        if (submission) {
          const savedUser = conv.messages.find(m => m.id === snapshot.user_message_id)
          if (savedUser && savedUser !== submission.user) conv.messages = conv.messages.filter(m => m !== submission.user)
          else submission.user.id = snapshot.user_message_id
        }
        if (assistant.content && run.thinking && submission) assistant.thinkingTime = +(snapshot.elapsed_seconds).toFixed(1)
        run.thinking = snapshot.status === 'running' && !assistant.content
        if (run.stopping && snapshot.status === 'running') void requestStop(id, run)
        if (currentId.value === id) scrollToBottom()
      }, value => { run.reconnecting = value }, ac.signal, submission ? { message: submission.text, kbOnly: submission.mode, projectId: submission.projectId } : undefined)
      if (disposed) return
      if (!outcome.started && submission) {
        conv.messages = conv.messages.filter(m => m !== submission.user && m !== submission.assistant)
        conv.hasMessages = submission.hadMessages
        failedAttempts.value[id] = { text: submission.text, mode: submission.mode, projectId: submission.projectId, requestId }
        handlePersistError('发送消息', new Error(outcome.error || '未能确认是否发送成功，请重试'))
      } else {
        delete failedAttempts.value[id]
        if (outcome.status === 'error' && outcome.error) {
          console.error('[回答失败]', outcome.error)
          showNotice(outcome.error, 'error')
        }
      }
    } finally {
      if (runs.value[id] === run) delete runs.value[id]
      controllers.delete(id)
      if (currentId.value === id) scrollToBottom()
    }
  }

  async function sendMessage(retryText?: string, answerMode = kbOnly.value, previousRequestId?: string) {
    const text = (retryText ?? input.value).trim()
    if (!text || isProcessing.value || conversationBusy.value || loading.value) return
    const convId = currentId.value
    const conv = conversations.value[convId]
    if (!conv || answerMode !== kbOnly.value || !conv.messagesLoaded || conv.messagesLoading) return
    if (!projectReady.value) {
      showNotice(PROJECT_REQUIRED_NOTICE, 'error')
      return
    }
    const hadMessages = conv.hasMessages
    conv.hasMessages = true
    conv.kbOnly = answerMode
    const user = reactive<Message>({ role: 'user', content: text, kbOnly: answerMode })
    const assistant = reactive<Message>({ role: 'assistant', content: '', status: 'streaming', kbOnly: answerMode })
    conv.messages.push(user, assistant)
    conv.updatedAt = new Date().toISOString()
    if (!hadMessages && conv.title === '新对话') {
      conv.title = text.length > 20 ? text.slice(0, 20) + '...' : text
      updateConversationTitle(convId, conv.title).catch(e => handlePersistError('更新标题', e))
    }
    if (retryText == null) { input.value = ''; resetInputHeight() }
    jumpToLatest()
    await observeGeneration(convId, previousRequestId || newRequestId(), { text, mode: answerMode, projectId: answerMode ? conv.project_id : null, user, assistant, hadMessages })
  }

  function onKeydown(e: KeyboardEvent) {
    if (e.isComposing || e.keyCode === 229) return
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage() }
  }

  function refreshCurrent() {
    if (!document.hidden && currentId.value && !runs.value[currentId.value]) void loadMessages()
  }
  onMounted(() => {
    void initConversations()
    window.addEventListener('focus', refreshCurrent)
  })
  onUnmounted(() => {
    disposed = true
    // 页面卸载只断开订阅，显式停止才调用取消接口。
    for (const controller of controllers.values()) controller.abort()
    window.removeEventListener('focus', refreshCurrent)
    if (thinkingTimer !== null) clearInterval(thinkingTimer)
  })
  return { input, inputEl, chatArea, current, currentId, convList, hasEmptyConv, isProcessing, processingIds, reconnecting, stopping, isThinking, thinkingSeconds, thinkingPhase, kbOnly, switchMode, newConversation, selectConversation, deleteConversation, sendMessage, stopStreaming, onKeydown, persistNotice, clearNotice,
    nextCursor, loadingMore, loadMoreConversations, loadMessages, hasEarlier, loading, loadError, initConversations, conversationBusy, renameConversation, following, onScroll, jumpToLatest, visibleMessages, messageOffset, showEarlier, retryMessage, canRetrySend, retryFailedSend,
    projectId, selectedProject, projectReady, selectProject, removeProjectConversations }
}
