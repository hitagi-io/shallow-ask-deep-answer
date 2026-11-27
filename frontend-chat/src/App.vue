<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { apiMe, confirmSession, getToken, suspendSession, verifiedTokenFor, TOKEN_KEY, setUnauthorizedHandler, type UserInfo } from './api'
import AuthView from './views/AuthView.vue'
import WorkspaceView from './views/WorkspaceView.vue'

const user = ref<UserInfo | null>(null)
let observedToken = getToken()
const checkingAuth = ref(!!observedToken)
const serviceUnavailable = ref(false)
const validatingSession = ref(false)
const authError = ref('')
const authSuccess = ref('')
let sessionRevision = 0

function endSession(error = '', success = '', nextToken: string | null = null) {
  sessionRevision += 1
  if (user.value) {
    sessionStorage.removeItem(`shallow_ask_deep_answer_mode_${user.value.id}`)
    sessionStorage.removeItem(`shallow_ask_deep_answer_project_${user.value.id}`)
  }
  suspendSession()
  observedToken = nextToken
  validatingSession.value = false
  user.value = null
  checkingAuth.value = !!nextToken
  serviceUnavailable.value = false
  authError.value = error
  authSuccess.value = success
  if (!nextToken && getToken()) void validateSession()
}
function onAuthenticated(authenticatedUser: UserInfo) {
  if (!onUserUpdated(authenticatedUser)) return
  sessionStorage.removeItem(`shallow_ask_deep_answer_mode_${authenticatedUser.id}`)
  sessionStorage.removeItem(`shallow_ask_deep_answer_project_${authenticatedUser.id}`)
  checkingAuth.value = false
  serviceUnavailable.value = false
  authError.value = ''
  authSuccess.value = ''
}
function onUserUpdated(updated: UserInfo) {
  const token = verifiedTokenFor(updated)
  if (!token) {
    void validateSession()
    return false
  }
  observedToken = token
  sessionRevision += 1
  validatingSession.value = false
  user.value = updated
  return true
}
setUnauthorizedHandler(() => endSession('登录已过期，请重新登录'))
let sessionTimer: ReturnType<typeof setInterval> | undefined
async function validateSession() {
  const token = getToken()
  if (token !== observedToken) {
    endSession(token ? '' : '登录状态已变更，请重新登录', '', token)
  }
  if (!token || validatingSession.value || (!checkingAuth.value && document.visibilityState !== 'visible')) return
  const revision = sessionRevision
  validatingSession.value = true
  try {
    const latest = await apiMe(token)
    if (revision !== sessionRevision || !confirmSession(token, latest)) return
    user.value = latest
    serviceUnavailable.value = false
  } catch (error) {
    if (revision !== sessionRevision) return
    // 只有 401 清除凭证；初次复核连接失败时等待恢复，不误显示登录页。
    if (token === getToken()) {
      if (!user.value) serviceUnavailable.value = true
      console.warn('[登录状态] 复核失败', error)
    }
  }
  finally {
    if (revision === sessionRevision) {
      validatingSession.value = false
      checkingAuth.value = false
      if (token !== getToken()) void validateSession()
    }
  }
}
function syncSession(event: StorageEvent) {
  if (event.storageArea === localStorage && (event.key === TOKEN_KEY || event.key === null)) void validateSession()
}
onMounted(() => {
  window.addEventListener('storage', syncSession)
  window.addEventListener('focus', validateSession)
  document.addEventListener('visibilitychange', validateSession)
  sessionTimer = setInterval(validateSession, 15000)
  void validateSession()
})
onUnmounted(() => {
  sessionRevision += 1
  setUnauthorizedHandler(null)
  clearInterval(sessionTimer)
  window.removeEventListener('storage', syncSession)
  window.removeEventListener('focus', validateSession)
  document.removeEventListener('visibilitychange', validateSession)
})
</script>

<template>
<div v-if="checkingAuth" class="auth-page auth-loading" role="status"><span class="loading-spinner" aria-hidden="true"></span><span>正在进入你的工作空间…</span></div>
<WorkspaceView v-else-if="user" :key="user.id" :user="user" @logout="endSession()" @user-updated="onUserUpdated" @account-deleted="endSession('', $event)" @password-changed="endSession('', '密码已修改，所有登录会话已失效，请使用新密码重新登录。')" />
<div v-else-if="serviceUnavailable" class="auth-page auth-loading">
  <span role="status">暂时无法连接服务</span>
  <button class="btn btn-secondary" :disabled="validatingSession" @click="validateSession">{{ validatingSession ? '连接中…' : '重试' }}</button>
</div>
<AuthView v-else :error="authError" :success="authSuccess" @authenticated="onAuthenticated" />
</template>
