import { ref, computed, watch, nextTick, onMounted, onUnmounted } from 'vue'
import { fetchConversations, fetchMessages, createConversationApi, updateConversationTitle, deleteConversationApi, streamChat, newRequestId, type Conversation, type Message } from '../api'
import { useNotice } from './useNotice'
import type { ConfirmAction } from './useConfirmation'

export function useChat(confirmAction: ConfirmAction = async () => false) {
  let disposed = false
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

  const { message: persistNotice, show: showNotice, clear: clearNotice } = useNotice(5000)
  function handlePersistError(op: string, e: unknown) {
    if (disposed) return
    console.error(`[操作失败] ${op}:`, e)
    const detail = e instanceof Error ? e.message : ''
    showNotice(detail ? `${op}失败：${detail}` : `${op}失败，请稍后重试`, 'error')
  }
  const conversations = ref<Record<string, Conversation>>({})
  const currentId = ref('')
  const input = ref('')
  const loading = ref(false)
  const loadError = ref('')
  const nextCursor = ref<string | null>(null)
  const loadingMore = ref(false)
  const conversationBusy = ref(false)
  const failedAttempt = ref<{ conversationId: string; text: string; mode: boolean; requestId: string } | null>(null)
  const canRetrySend = computed(() => failedAttempt.value?.conversationId === currentId.value)
  const following = ref(true)
  const visibleCount = ref(50)
  let pendingFrame: number | null = null
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
  const suggestions = computed(() => kbOnly.value ? [
    { label: '总结资料中的关键结论', prompt: '请总结【请填写文档名称或资料主题】中的关键结论，重点关注【请填写你关注的问题】。' },
    { label: '查找相关规定与操作流程', prompt: '请查找关于【请填写具体事项】的相关规定与操作流程。' },
    { label: '对比不同文档中的观点', prompt: '请对比【请填写文档一名称】与【请填写文档二名称】在【请填写对比主题】上的观点，说明异同。' },
  ] : [
    { label: '用易懂的方式解释概念', prompt: '请用通俗易懂的语言解释【请填写想了解的概念】，并结合具体例子说明。' },
    { label: '起草一段文字', prompt: '请帮我写一份【请填写文案类型】，主题是【请填写主题】，面向【请填写读者】，语气希望【请填写语气或风格】。' },
    { label: '梳理思路与行动方案', prompt: '我想实现【请填写目标】，目前的情况是【请填写背景与限制】。请帮我梳理思路，并给出可执行的步骤。' },
  ])
  function useSuggestion(text: string) {
    input.value = text
    nextTick(() => {
      const textarea = inputEl.value
      if (!textarea) return
      textarea.style.height = 'auto'
      textarea.style.height = `${Math.min(textarea.scrollHeight, 120)}px`
      textarea.focus()
      const placeholder = /【[^】]+】/.exec(text)
      if (placeholder) textarea.setSelectionRange(placeholder.index, placeholder.index + placeholder[0].length)
    })
  }

  async function initConversations() {
    if (loading.value) return
    loading.value = true
    loadError.value = ''
    try {
      const page = await fetchConversations()
      if (disposed) return
      nextCursor.value = page.next_cursor
      const loaded: Record<string, Conversation> = {}
      for (const c of page.conversations) loaded[c.id] = { ...c, messages: [] }
      if (Object.keys(loaded).length === 0) {
        const conv = await createConversationApi('新对话')
        loaded[conv.id] = conv
      }
      if (disposed) return
      conversations.value = loaded
      currentId.value = Object.keys(loaded)[0] || ''
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
        if (!conversations.value[c.id]) conversations.value[c.id] = { ...c, messages: [] }
      }
      nextCursor.value = page.next_cursor
    } catch (e) { handlePersistError('加载更多会话', e) }
    finally { loadingMore.value = false }
  }

  async function loadMessages(id = currentId.value, earlier = false) {
    const conv = conversations.value[id]
    if (!conv || conv.messagesLoading || (earlier && (!conv.nextBefore || isProcessing.value))) return
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
      if (!earlier && currentId.value === id) jumpToLatest()
    } catch (e) {
      conv.messagesError = e instanceof Error ? e.message : '消息加载失败'
    } finally { conv.messagesLoading = false }
  }

  watch(isThinking, (val) => {
    if (val) {
      thinkingSeconds.value = 0
      const tick = () => {
        thinkingSeconds.value = +((Date.now() - thinkingStartTime) / 1000).toFixed(1)
        thinkingTimer = window.setTimeout(tick, 100)
      }
      thinkingTimer = window.setTimeout(tick, 100)
    } else {
      if (thinkingTimer != null) { clearTimeout(thinkingTimer); thinkingTimer = null }
    }
  })

  const current = computed(() => conversations.value[currentId.value])
  const convList = computed(() => Object.values(conversations.value).sort((a, b) => new Date(b.updatedAt || 0).getTime() - new Date(a.updatedAt || 0).getTime()))
  const hasEmptyConv = computed(() => convList.value.some(c => c.messagesLoaded && c.messages.length === 0))
  const messageOffset = computed(() => Math.max(0, (current.value?.messages.length || 0) - visibleCount.value))
  const visibleMessages = computed(() => current.value?.messages.slice(messageOffset.value) || [])
  const hasEarlier = computed(() => messageOffset.value > 0 || !!current.value?.nextBefore)
  async function showEarlier() {
    const conv = current.value
    if (!conv || conv.messagesLoading || isProcessing.value) return
    const id = currentId.value
    const el = chatArea.value
    const height = el?.scrollHeight || 0
    const top = el?.scrollTop || 0
    following.value = false
    if (messageOffset.value > 0) visibleCount.value += 50
    else {
      const count = conv.messages.length
      await loadMessages(id, true)
      if (currentId.value !== id) return
      visibleCount.value += conv.messages.length - count
    }
    await nextTick()
    if (el && currentId.value === id) el.scrollTop = top + el.scrollHeight - height
  }
  function onScroll() {
    const el = chatArea.value
    if (el) following.value = el.scrollHeight - el.scrollTop - el.clientHeight < 80
  }
  function jumpToLatest() { following.value = true; scrollToBottom() }
  watch(currentId, id => {
    visibleCount.value = 50
    if (conversations.value[id] && !conversations.value[id].messagesLoaded) void loadMessages(id)
    jumpToLatest()
  })

  function scrollToBottom() {
    nextTick(() => {
      if (following.value && chatArea.value) chatArea.value.scrollTop = chatArea.value.scrollHeight
    })
  }

  async function newConversation() {
    if (isProcessing.value || conversationBusy.value || loading.value) return
    const empty = convList.value.find(c => c.messagesLoaded && c.messages.length === 0)
    if (empty) { currentId.value = empty.id; return }
    conversationBusy.value = true
    try {
      const conv = await createConversationApi('新对话')
      conv.updatedAt = new Date().toISOString()
      conversations.value[conv.id] = conv
      currentId.value = conv.id
    } catch (e) {
      handlePersistError('创建会话', e)
    } finally { conversationBusy.value = false }
  }

  function selectConversation(id: string) {
    if (isProcessing.value) return
    currentId.value = id
  }

  async function deleteConversation(id: string) {
    if (isProcessing.value || conversationBusy.value || !await confirmAction('删除此对话及全部消息？此操作无法撤销。')) return
    conversationBusy.value = true
    // 先由服务端确认删除：运行中的会话会返回 409，本地不能抢先移除
    try {
      await deleteConversationApi(id)
    } catch (e) {
      handlePersistError('删除会话', e)
      return
    } finally { conversationBusy.value = false }
    delete conversations.value[id]
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
  function retryMessage(index: number) {
    const question = current.value?.messages.slice(0, index).reverse().find(m => m.role === 'user')
    if (question && !isProcessing.value) void sendMessage(question.content, current.value?.messages[index].kbOnly ?? kbOnly.value)
  }
  function retryFailedSend() {
    const attempt = failedAttempt.value
    if (attempt && attempt.conversationId === currentId.value) return sendMessage(attempt.text, attempt.mode, attempt.requestId)
  }

  async function sendMessage(retryText?: string, answerMode = kbOnly.value, previousRequestId?: string) {
    const text = (retryText ?? input.value).trim()
    if (!text || isProcessing.value || conversationBusy.value || loading.value) return
    const convId = currentId.value
    const conv = conversations.value[convId]
    if (!conv || !conv.messagesLoaded || conv.messagesLoading) return

    // 本地先乐观展示；服务端在/stream 接口中原子写入用户消息，客户端不再单独提交
    conv.messages.push({ role: 'user', content: text })
    const userIdx = conv.messages.length - 1
    conv.updatedAt = new Date().toISOString()
    if (conv.messages.length === 1 && conv.title === '新对话') {
      conv.title = text.length > 20 ? text.slice(0, 20) + '...' : text
      updateConversationTitle(convId, conv.title).catch(e => handlePersistError('更新标题', e))
    }
    if (retryText == null) { input.value = ''; resetInputHeight() }
    thinkingStartTime = Date.now()
    thinkingPhase.value = ''
    isProcessing.value = true
    isThinking.value = true
    jumpToLatest()

    const assistantMsg: Message = { role: 'assistant', content: '', status: 'streaming', kbOnly: answerMode }
    conv.messages.push(assistantMsg)
    const idx = conv.messages.length - 1

    const captureThinkingTime = () => {
      if (conv.messages[idx].thinkingTime == null) {
        conv.messages[idx].thinkingTime = +((Date.now() - thinkingStartTime) / 1000).toFixed(1)
      }
    }

    let buffered = ''
    let replacing = false
    const flushAnswer = () => {
      if (pendingFrame !== null) { cancelAnimationFrame(pendingFrame); pendingFrame = null }
      if (!conv.messages[idx]) { buffered = ''; replacing = false; return }
      if (replacing) conv.messages[idx].content = buffered
      else conv.messages[idx].content += buffered
      buffered = ''
      replacing = false
      scrollToBottom()
    }
    const updateAnswer = (text: string, replace = false) => {
      if (isThinking.value) { captureThinkingTime(); isThinking.value = false }
      buffered = replace ? text : buffered + text
      replacing ||= replace
      if (pendingFrame === null) pendingFrame = requestAnimationFrame(flushAnswer)
    }

    // 上一次可能仍在途（正常情况下已被 isProcessing 挡住），先取消再开启新的
    streamAbort?.abort()
    const ac = new AbortController()
    streamAbort = ac
    // 幂等键：网络重试复用同一个值，服务端不会重复写入消息或重复生成
    const requestId = previousRequestId || newRequestId()

    try {
      const outcome = await streamChat(
        convId, requestId, text, answerMode,
        chunk => updateAnswer(chunk),
        text => updateAnswer(text, true),
        phase => { if (isThinking.value) thinkingPhase.value = PHASE_LABELS[phase] || '' },
        ac.signal,
      )
      flushAnswer()
      captureThinkingTime()

      if (!outcome.started) {
        // 服务端没有接受本次请求（会话已删除、会话被占用或校验失败）：
        // 回滚本地乐观消息，避免展示数据库中不存在的问答
        conv.messages.splice(userIdx, 2)
        if (!ac.signal.aborted) failedAttempt.value = { conversationId: convId, text, mode: answerMode, requestId }
        if (!ac.signal.aborted) handlePersistError('发送消息', new Error(outcome.error || '请求未被服务端接受'))
        return
      }
      if (failedAttempt.value?.conversationId === convId) failedAttempt.value = null

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
    } catch (e) {
      captureThinkingTime()
      if (ac.signal.aborted) {
        conv.messages[idx].status = 'cancelled'
      } else {
        conv.messages[idx].status = 'error'
        if (!conv.messages[idx].content) conv.messages[idx].content = '网络错误，请检查连接后重试'
      }
      handlePersistError('发送消息', e)
    } finally {
      flushAnswer()
      isProcessing.value = false
      isThinking.value = false
      thinkingPhase.value = ''
      scrollToBottom()
      if (streamAbort === ac) streamAbort = null
    }
  }

  function onKeydown(e: KeyboardEvent) {
    if (e.isComposing || e.keyCode === 229) return
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage() }
  }

  onMounted(initConversations)
  onUnmounted(() => {
    disposed = true
    stopStreaming()
    if (thinkingTimer !== null) clearTimeout(thinkingTimer)
    if (pendingFrame !== null) cancelAnimationFrame(pendingFrame)
  })
  return { input, inputEl, chatArea, current, currentId, convList, hasEmptyConv, isProcessing, isThinking, thinkingSeconds, thinkingPhase, kbOnly, suggestions, useSuggestion, newConversation, selectConversation, deleteConversation, sendMessage, stopStreaming, onKeydown, persistNotice, clearNotice,
    nextCursor, loadingMore, loadMoreConversations, loadMessages, hasEarlier, loading, loadError, initConversations, conversationBusy, renameConversation, following, onScroll, jumpToLatest, visibleMessages, messageOffset, showEarlier, retryMessage, canRetrySend, retryFailedSend }
}
