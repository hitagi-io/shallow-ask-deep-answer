<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { apiMe, clearAuth, getStoredUser, setUnauthorizedHandler, type UserInfo } from './api'
import AuthView from './views/AuthView.vue'
import WorkspaceView from './views/WorkspaceView.vue'

const user = ref<UserInfo | null>(null)
const checkingAuth = ref(!!getStoredUser())
const serviceUnavailable = ref(false)
const validatingSession = ref(false)
const authError = ref('')
const authSuccess = ref('')
let sessionRevision = 0

function endSession(error = '', success = '') {
  sessionRevision += 1
  if (user.value) sessionStorage.removeItem(`shallow_ask_deep_answer_mode_${user.value.id}`)
  clearAuth()
  user.value = null
  checkingAuth.value = false
  serviceUnavailable.value = false
  authError.value = error
  authSuccess.value = success
}
function onAuthenticated(authenticatedUser: UserInfo) {
  sessionRevision += 1
  sessionStorage.removeItem(`shallow_ask_deep_answer_mode_${authenticatedUser.id}`)
  user.value = authenticatedUser
  checkingAuth.value = false
  serviceUnavailable.value = false
  authError.value = ''
  authSuccess.value = ''
}
setUnauthorizedHandler(() => endSession('登录已过期，请重新登录'))
let sessionTimer: ReturnType<typeof setInterval> | undefined
async function validateSession() {
  if (validatingSession.value || (!checkingAuth.value && document.visibilityState !== 'visible')) return
  if (!getStoredUser()) {
    if (user.value || serviceUnavailable.value) endSession('登录状态已变更，请重新登录')
    checkingAuth.value = false
    return
  }
  const current = user.value
  const revision = sessionRevision
  validatingSession.value = true
  try {
    const latest = await apiMe()
    if (revision !== sessionRevision) return
    if (user.value === current) user.value = latest
    serviceUnavailable.value = false
  } catch (error) {
    if (revision !== sessionRevision) return
    // 只有 401 清除凭证；初次复核连接失败时等待恢复，不误显示登录页。
    if (getStoredUser()) {
      if (!user.value) serviceUnavailable.value = true
      console.warn('[登录状态] 复核失败', error)
    }
  }
  finally {
    validatingSession.value = false
    if (revision === sessionRevision) checkingAuth.value = false
  }
}
function syncLogout(event: StorageEvent) {
  if (event.storageArea === localStorage && (user.value || checkingAuth.value || serviceUnavailable.value) && !getStoredUser()) {
    endSession('登录状态已变更，请重新登录')
  }
}
onMounted(() => {
  window.addEventListener('storage', syncLogout)
  window.addEventListener('focus', validateSession)
  document.addEventListener('visibilitychange', validateSession)
  sessionTimer = setInterval(validateSession, 15000)
  if (checkingAuth.value) void validateSession()
})
onUnmounted(() => {
  sessionRevision += 1
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
<div v-else-if="serviceUnavailable" class="auth-page auth-loading">
  <span role="status">暂时无法连接服务</span>
  <button class="btn btn-secondary" :disabled="validatingSession" @click="validateSession">{{ validatingSession ? '连接中…' : '重试' }}</button>
</div>
<AuthView v-else :error="authError" :success="authSuccess" @authenticated="onAuthenticated" />
</template>
