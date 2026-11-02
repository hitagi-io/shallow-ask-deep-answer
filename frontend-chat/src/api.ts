export type MessageStatus = 'streaming' | 'completed' | 'error' | 'cancelled'

export interface Message {
  /** 服务端生成的消息 ID；本地乐观消息在服务端确认前为空 */
  id?: number
  role: 'user' | 'assistant'
  content: string
  thinkingTime?: number
  status?: MessageStatus
  kbOnly?: boolean
}

export interface MessageHistory {
  messages: Message[]
  messagesLoaded?: boolean
  nextBefore?: number | null
  messagesLoading?: boolean
  messagesError?: string
}

export interface ConversationSummary {
  id: string
  title: string
  updatedAt?: string
  /** 是否已有消息，用于识别唯一的空白会话。 */
  hasMessages: boolean
  /** 首次发送后固定的会话模式；空白会话可在本地切换。 */
  kbOnly: boolean | null
}

export interface Conversation extends ConversationSummary, MessageHistory {}

export function withEmptyHistory(summary: ConversationSummary): Conversation {
  return { ...summary, messages: [], messagesLoaded: !summary.hasMessages, nextBefore: null }
}

export interface UserInfo {
  id: number
  username: string
  role: 'user' | 'admin'
}

export interface FileItem {
  name: string
  path: string
  /** 是否已入库（已构建进向量库） */
  indexed: boolean
}

// ---------- auth helpers ----------

const TOKEN_KEY = 'deepagent_token'
const USER_KEY = 'deepagent_user'

function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

/** 读取本地登录快照；当前身份和权限仍需通过 apiMe 向服务端验证。 */
export function getStoredUser(): UserInfo | null {
  const raw = localStorage.getItem(USER_KEY)
  if (!raw) return null
  try {
    return JSON.parse(raw) as UserInfo
  } catch {
    // localStorage 被写坏时不能让应用初始化整体报错（会白屏）：清掉坏值按未登录处理
    localStorage.removeItem(USER_KEY)
    return null
  }
}

function saveAuth(token: string, user: UserInfo) {
  localStorage.setItem(TOKEN_KEY, token)
  localStorage.setItem(USER_KEY, JSON.stringify(user))
}

export function clearAuth() {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(USER_KEY)
}

function authHeaders(): Record<string, string> {
  const token = getToken()
  return token ? { Authorization: `Bearer ${token}` } : {}
}

// 401 统一回调：由应用层注册（清空登录态并展示登录页）
let unauthorizedHandler: (() => void) | null = null

export function setUnauthorizedHandler(fn: (() => void) | null) {
  unauthorizedHandler = fn
}

/**
 * 已鉴权接口的请求封装：任一请求返回 401 即视为登录态失效，
 * 统一清除本地凭证并通知应用层回到登录页。
 * 登录接口不使用它——那里的 401 表示账号密码错误，而非会话过期。
 */
async function apiFetch(input: string, init?: RequestInit): Promise<Response> {
  const token = getToken()
  const res = await fetch(input, init)
  if (res.status === 401 && token === getToken()) {
    clearAuth()
    unauthorizedHandler?.()
  }
  return res
}

/**
 * 取服务端错误文案：FastAPI 的 detail 在业务错误时是字符串，
 * 在 422 校验失败时是数组/对象，只有字符串可直接展示，否则回落到兜底文案。
 */
async function errorMessage(res: Response, fallback: string): Promise<string> {
  const data = (await res.json().catch(() => null)) as { detail?: unknown } | null
  const detail = data?.detail
  return typeof detail === 'string' && detail ? detail : fallback
}

// ---------- auth API ----------

export async function apiRegister(username: string, password: string): Promise<void> {
  const res = await fetch('/api/auth/register', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
  if (!res.ok) {
    throw new Error(await errorMessage(res, '注册失败'))
  }
}

export async function apiLogin(username: string, password: string): Promise<UserInfo> {
  const res = await fetch('/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
  if (!res.ok) {
    throw new Error(await errorMessage(res, '登录失败'))
  }
  const data = await res.json()
  saveAuth(data.token, data.user)
  return data.user
}

export async function apiMe(): Promise<UserInfo> {
  const res = await apiFetch('/api/auth/me', { headers: authHeaders() })
  if (!res.ok) throw new Error('未登录')
  const data = await res.json()
  return data.user
}

// ---------- conversation API ----------

export async function fetchConversations(cursor?: string | null): Promise<{
  conversations: ConversationSummary[]; next_cursor: string | null
}> {
  const query = new URLSearchParams({ limit: '20' })
  if (cursor) query.set('cursor', cursor)
  const res = await apiFetch(`/api/conversations?${query}`, { headers: authHeaders() })
  if (!res.ok) throw new Error(await errorMessage(res, '获取会话列表失败'))
  return res.json()
}

export async function fetchMessages(id: string, before?: number | null): Promise<{
  messages: Message[]; next_before: number | null; kbOnly: boolean | null; active_generation: { request_id: string } | null
}> {
  const query = new URLSearchParams({ limit: '50' })
  if (before != null) query.set('before', String(before))
  const res = await apiFetch(`/api/conversations/${encodeURIComponent(id)}/messages?${query}`, { headers: authHeaders() })
  if (!res.ok) throw new Error(await errorMessage(res, '获取消息失败'))
  return res.json()
}

export async function createConversationApi(title: string): Promise<Conversation> {
  const res = await apiFetch('/api/conversations', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ title }),
  })
  if (!res.ok) throw new Error(await errorMessage(res, '创建会话失败'))
  const summary: ConversationSummary = await res.json()
  return withEmptyHistory(summary)
}

export async function updateConversationTitle(id: string, title: string): Promise<void> {
  const res = await apiFetch(`/api/conversations/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ title }),
  })
  if (!res.ok) throw new Error(await errorMessage(res, '更新标题失败'))
}

export async function deleteConversationApi(id: string): Promise<void> {
  const res = await apiFetch(`/api/conversations/${encodeURIComponent(id)}`, {
    method: 'DELETE',
    headers: authHeaders(),
  })
  if (!res.ok) throw new Error(await errorMessage(res, '删除会话失败'))
}

// ---------- chat API ----------

/** 生成请求幂等键；非安全上下文（HTTP 内网访问）没有 randomUUID 时用时间戳兜底 */
export function newRequestId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`
}

export interface GenerationSnapshot {
  request_id: string
  status: 'running' | 'completed' | 'failed' | 'cancelled'
  assistant_content: string | null
  assistant_message_id: number
  user_message_id: number
  kb_only: boolean
  elapsed_seconds: number
  phase: string
  error: string | null
}

export interface StreamOutcome {
  status: 'completed' | 'error' | 'cancelled'
  started: boolean
  error?: string
}

function generationPath(conversationId: string, requestId: string) {
  return `/api/conversations/${encodeURIComponent(conversationId)}/generations/${encodeURIComponent(requestId)}`
}

export async function cancelGeneration(conversationId: string, requestId: string): Promise<void> {
  const res = await apiFetch(`${generationPath(conversationId, requestId)}/cancel`, {
    method: 'POST', headers: authHeaders(),
  })
  if (!res.ok) throw new Error(await errorMessage(res, '停止生成失败'))
}

function reconnectDelay(signal: AbortSignal): Promise<void> {
  return new Promise(resolve => {
    const finish = () => { clearTimeout(timer); signal.removeEventListener('abort', finish); resolve() }
    const timer = setTimeout(finish, 1000)
    signal.addEventListener('abort', finish, { once: true })
    if (signal.aborted) finish()
  })
}

/** POST 只创建一次任务；断线后使用 GET 重新订阅数据库快照，不重复提交问题。 */
export async function streamChat(
  conversationId: string,
  requestId: string,
  onSnapshot: (snapshot: GenerationSnapshot) => void,
  onConnection: (reconnecting: boolean) => void,
  signal: AbortSignal,
  submission?: { message: string; kbOnly: boolean },
): Promise<StreamOutcome> {
  let started = !submission
  let submit = submission
  let missingAttempts = 0
  while (!signal.aborted) {
    let reader: ReadableStreamDefaultReader<Uint8Array> | undefined
    try {
      const initial = submit
      submit = undefined
      const res = await apiFetch(initial ? '/api/chat/stream' : `${generationPath(conversationId, requestId)}/stream`, {
        method: initial ? 'POST' : 'GET',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: initial ? JSON.stringify({ message: initial.message, thread_id: conversationId, request_id: requestId, kb_only: initial.kbOnly }) : undefined,
        signal,
      })
      if (!res.ok) {
        // 首次 POST 回执丢失时，给登记事务留出完成时间，再确认任务是否存在。
        if (!initial && res.status === 404 && !started && ++missingAttempts < 3) throw new Error('等待任务登记')
        if (res.status >= 500) throw new Error('服务暂时不可用')
        return { status: 'error', started, error: await errorMessage(res, `请求失败: ${res.status}`) }
      }
      started = true
      reader = res.body?.getReader()
      if (!reader) throw new Error('无法读取响应流')
      const decoder = new TextDecoder()
      let buffer = ''
      const handleLine = (line: string): StreamOutcome | null => {
        if (!line.startsWith('data: ')) return null
        const data = JSON.parse(line.slice(6))
        if (data.type === 'error') return { status: 'error', started, error: data.error }
        if (data.type !== 'snapshot') return null
        onConnection(false)
        onSnapshot(data as GenerationSnapshot)
        if (data.status === 'running') return null
        return { status: data.status === 'completed' ? 'completed' : data.status === 'cancelled' ? 'cancelled' : 'error', started, error: data.error }
      }
      while (!signal.aborted) {
        const { done, value } = await reader.read()
        buffer += done ? decoder.decode() : decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() || ''
        if (done && buffer) lines.push(buffer)
        for (const line of lines) {
          const outcome = handleLine(line)
          if (outcome) return outcome
        }
        if (done) break
      }
    } catch {
      // 网络断线与停止生成是不同操作；保持会话运行态并重新订阅。
    } finally {
      await reader?.cancel().catch(() => {})
    }
    if (!signal.aborted) {
      onConnection(true)
      await reconnectDelay(signal)
    }
  }
  return { status: 'cancelled', started }
}

// ---------- knowledge API ----------

export async function fetchFiles(): Promise<FileItem[]> {
  const res = await apiFetch('/api/files', { headers: authHeaders() })
  if (!res.ok) throw new Error(await errorMessage(res, '获取文件列表失败'))
  const data = await res.json()
  return data.files
}

export async function uploadFiles(files: File[], onUploaded?: () => void, signal?: AbortSignal): Promise<{
  uploaded: { name: string; overwritten: boolean; indexed: boolean; error: string | null }[]
  errors: { name: string; error: string }[]
}> {
  const form = new FormData()
  files.forEach(f => form.append('files', f))
  const token = getToken()
  const res = await new Promise<Response>((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', '/api/files/upload')
    for (const [key, value] of Object.entries(authHeaders())) xhr.setRequestHeader(key, value)
    const abort = () => xhr.abort()
    xhr.upload.onload = () => onUploaded?.()
    xhr.onload = () => {
      if (xhr.status === 401 && getToken() === token) { clearAuth(); unauthorizedHandler?.() }
      resolve(new Response(xhr.responseText, { status: xhr.status }))
    }
    xhr.onerror = () => reject(new Error('上传连接失败，请检查网络'))
    xhr.onabort = () => reject(new DOMException('上传已取消', 'AbortError'))
    xhr.onloadend = () => signal?.removeEventListener('abort', abort)
    if (signal?.aborted) { reject(new DOMException('上传已取消', 'AbortError')); return }
    signal?.addEventListener('abort', abort, { once: true })
    xhr.send(form)
  })
  if (!res.ok) throw new Error(await errorMessage(res, '上传失败'))
  return res.json()
}

export async function deleteFile(name: string): Promise<void> {
  const res = await apiFetch(`/api/files/${encodeURIComponent(name)}`, { method: 'DELETE', headers: authHeaders() })
  if (!res.ok) throw new Error(await errorMessage(res, '删除失败'))
}

/** 为尚未记录的知识库内容补建索引。 */
export async function buildKnowledgeIndex(): Promise<{ message: string; failures: { file: string; error: string }[] }> {
  const res = await apiFetch('/api/vector/build', { method: 'POST', headers: authHeaders() })
  if (!res.ok) throw new Error(await errorMessage(res, '构建失败'))
  return res.json()
}

export interface HealthIssue {
  type: string
  severity: 'warn' | 'error'
  title: string
  detail?: string
  count: number
  items: string[]
}

export interface HealthReport {
  checked_at: string | null
  healthy: boolean
  milvus_error?: string | null
  manifest_error?: string | null
  stats: { files: number; indexed_files: number; chunks: number }
  issues: HealthIssue[]
  repair?: {
    ghost_deleted: string[]
    ghost_errors: string[]
    manifest_rebuilt: boolean
    re_indexed: string[]
    re_index_failures: { file: string; error: string }[]
  }
}

export async function fetchIndexHealth(refresh = false): Promise<HealthReport> {
  const res = await apiFetch(`/api/index/health${refresh ? '?refresh=1' : ''}`, { headers: authHeaders() })
  if (!res.ok) throw new Error(await errorMessage(res, '获取索引健康状态失败'))
  return res.json()
}

export async function repairIndexHealth(): Promise<HealthReport> {
  const res = await apiFetch('/api/index/health/repair', { method: 'POST', headers: authHeaders() })
  if (!res.ok) throw new Error(await errorMessage(res, '修复失败'))
  return res.json()
}
