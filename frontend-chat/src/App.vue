<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { apiMe, clearAuth, getStoredUser, setUnauthorizedHandler, type UserInfo } from './api'
import AuthView from './components/AuthView.vue'
import WorkspaceView from './components/WorkspaceView.vue'

const user = ref<UserInfo | null>(null)
const checkingAuth = ref(!!getStoredUser())
const authError = ref('')

function endSession(error = '') {
  clearAuth()
  user.value = null
  authError.value = error
  localStorage.setItem('deepagent_mode', 'chat')
}
setUnauthorizedHandler(() => endSession('登录已过期，请重新登录'))
onMounted(async () => {
  if (!checkingAuth.value) return
  try { user.value = await apiMe() }
  catch { clearAuth() }
  finally { checkingAuth.value = false }
})
onUnmounted(() => setUnauthorizedHandler(null))
</script>

<template>
<div v-if="checkingAuth" class="auth-page" role="status">正在验证登录状态...</div>
<WorkspaceView v-else-if="user" :key="user.id" :user="user" @logout="endSession()" />
<AuthView v-else :error="authError" @authenticated="user = $event; authError = ''" />
</template>
