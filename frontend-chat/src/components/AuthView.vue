<script setup lang="ts">
import { ref, watch } from 'vue'
import { apiLogin, apiRegister, type UserInfo } from '../api'
import BrandMark from './BrandMark.vue'
import AppNotice from './AppNotice.vue'
const props = defineProps<{ error: string }>()
const emit = defineEmits<{ authenticated: [user: UserInfo] }>()
const authMode = ref<'login' | 'register'>('login')
const authUsername = ref('')
const authPassword = ref('')
const authPassword2 = ref('')
const authError = ref(props.error)
const authSuccess = ref('')
const authLoading = ref(false)

watch(() => props.error, value => { authError.value = value })
async function handleSubmit() {
  if (authLoading.value) return
  authError.value = ''
  authSuccess.value = ''
  if (!authUsername.value.trim() || !authPassword.value) {
    authError.value = '请填写用户名和密码'
    return
  }
  if (authMode.value === 'register') {
    if (authPassword.value !== authPassword2.value) {
      authError.value = '两次输入的密码不一致'
      return
    }
    if (authPassword.value.length < 10 || !/\d/.test(authPassword.value) || !/[a-zA-Z]/.test(authPassword.value) || !/[^0-9a-zA-Z\s]/.test(authPassword.value)) {
      authError.value = '密码需至少10个字符，且包含数字、英文字母和特殊符号'
      return
    }
  }
  authLoading.value = true
  try {
    if (authMode.value === 'login') {
      emit('authenticated', await apiLogin(authUsername.value.trim(), authPassword.value))
      return
    }
    await apiRegister(authUsername.value.trim(), authPassword.value)
    // 注册成功后切换到登录页
    authMode.value = 'login'
    authSuccess.value = '注册成功，请登录'
    authPassword.value = ''
    authPassword2.value = ''
  } catch (e) {
    authError.value = (e as Error).message
  } finally {
    authLoading.value = false
  }
}
</script>

<template>
  <div class="auth-page">
    <div class="auth-card">
      <BrandMark class="auth-mark" />
      <h1 class="auth-title">浅问深答</h1>

      <div class="auth-tabs">
        <button :disabled="authLoading" :class="{ active: authMode === 'login' }" @click="authMode = 'login'; authError = ''; authSuccess = ''">登录</button>
        <button :disabled="authLoading" :class="{ active: authMode === 'register' }" @click="authMode = 'register'; authError = ''; authSuccess = ''">注册</button>
      </div>

      <AppNotice v-if="authSuccess" class="auth-notice" tone="success">{{ authSuccess }}</AppNotice>
      <AppNotice v-if="authError" class="auth-notice" tone="error">{{ authError }}</AppNotice>

      <form @submit.prevent="handleSubmit" class="auth-form">
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
          />
        <input
          v-if="authMode === 'register'"
          v-model="authPassword2"
          type="password"
          placeholder="确认密码" aria-label="确认密码"
          autocomplete="new-password"
          :disabled="authLoading"
          />
        <button type="submit" class="btn btn-primary btn-auth" :disabled="authLoading">
          {{ authLoading ? '处理中...' : (authMode === 'login' ? '登录' : '注册') }}
        </button>
      </form>
    </div>
  </div>
</template>

<style scoped>
.auth-card { width: 100%; max-width: 390px; padding: 36px; border: 1px solid var(--line); border-radius: 18px; background: white; box-shadow: 0 12px 50px #00000004; }
.auth-mark { display: block; width: 36px; height: 36px; margin: 0 auto 20px; }
.auth-title { font-size: 26px; font-weight: 600; text-align: center; margin: 0; letter-spacing: -.6px; }
.auth-tabs { display: flex; padding: 4px; background: var(--surface); border-radius: 8px; margin: 30px 0 22px; }
.auth-tabs button { flex: 1; padding: 8px; font-size: 13px; color: var(--muted); border-radius: 5px; }
.auth-tabs button.active { color: var(--text); background: white; box-shadow: 0 1px 3px #0000000c; }
.auth-form { display: flex; flex-direction: column; gap: 12px; }
.auth-form input { width: 100%; padding: 12px; border: 1px solid var(--line); border-radius: var(--radius-control); font-size: 14px; background: var(--bg); }
.btn-auth { margin-top: 6px; padding: 12px; }
.auth-notice { margin-bottom: 16px; }
@media (max-width: 760px) {
  .auth-card { padding: 28px 24px; }
}
</style>
