<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { apiMe, clearAuth, getStoredUser, setUnauthorizedHandler, type UserInfo } from './api'
import AuthView from './components/AuthView.vue'
import WorkspaceView from './components/WorkspaceView.vue'

const user = ref<UserInfo | null>(null)
const checkingAuth = ref(!!getStoredUser())
const authError = ref('')
const authSuccess = ref('')

function endSession(error = '', success = '') {
  if (user.value) sessionStorage.removeItem(`shallow_ask_deep_answer_mode_${user.value.id}`)
  clearAuth()
  user.value = null
  authError.value = error
  authSuccess.value = success
}
function onAuthenticated(authenticatedUser: UserInfo) {
  sessionStorage.removeItem(`shallow_ask_deep_answer_mode_${authenticatedUser.id}`)
  user.value = authenticatedUser
  authError.value = ''
  authSuccess.value = ''
}
setUnauthorizedHandler(() => endSession('登录已过期，请重新登录'))
let validatingSession = false
let sessionTimer: ReturnType<typeof setInterval> | undefined
async function validateSession() {
  if (!user.value || validatingSession || document.visibilityState !== 'visible') return
  const current = user.value
  validatingSession = true
  try {
    const latest = await apiMe()
    if (user.value === current) user.value = latest
  } catch { /* 401 is handled centrally; transient network failures do not log users out. */ }
  finally { validatingSession = false }
}
function syncLogout(event: StorageEvent) {
  if (event.storageArea === localStorage && user.value && !getStoredUser()) {
    endSession('登录状态已变更，请重新登录')
  }
}
onMounted(async () => {
  window.addEventListener('storage', syncLogout)
  window.addEventListener('focus', validateSession)
  document.addEventListener('visibilitychange', validateSession)
  sessionTimer = setInterval(validateSession, 15000)
  if (!checkingAuth.value) return
  try { user.value = await apiMe() }
  catch { clearAuth() }
  finally { checkingAuth.value = false }
})
onUnmounted(() => {
  setUnauthorizedHandler(null)
  clearInterval(sessionTimer)
  window.removeEventListener('storage', syncLogout)
  window.removeEventListener('focus', validateSession)
  document.removeEventListener('visibilitychange', validateSession)
})
</script>

<template>
<div v-if="checkingAuth" class="auth-page auth-loading" role="status"><span class="loading-spinner" aria-hidden="true"></span><span>正在进入你的工作空间…</span></div>
<WorkspaceView v-else-if="user" :key="user.id" :user="user" @logout="endSession()" @user-updated="user = $event" @account-deleted="endSession('', '账号及其项目、文档、索引和对话已注销删除。')" @password-changed="endSession('', '密码已修改，所有登录会话已失效，请使用新密码重新登录。')" />
<AuthView v-else :error="authError" :success="authSuccess" @authenticated="onAuthenticated" />
</template>
