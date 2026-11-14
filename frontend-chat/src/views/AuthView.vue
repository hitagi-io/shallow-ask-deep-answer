<script setup lang="ts">
import { ref, watch } from 'vue'
import { apiLogin, apiRegister, type UserInfo } from '../api'
import BrandMark from '../components/common/BrandMark.vue'
import AppNotice from '../components/common/AppNotice.vue'
import AppIcon from '../components/common/AppIcon.vue'
const props = defineProps<{ error: string; success?: string }>()
const emit = defineEmits<{ authenticated: [user: UserInfo] }>()
const authMode = ref<'login' | 'register'>('login')
const authUsername = ref('')
const authPassword = ref('')
const authPassword2 = ref('')
const authError = ref(props.error)
const authSuccess = ref(props.success || '')
const authLoading = ref(false)

watch(() => props.error, value => { authError.value = value })
watch(() => props.success, value => { authSuccess.value = value || '' })
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
    <div class="auth-layout">
      <div class="auth-brand"><span class="brand-tile"><BrandMark /></span><span>浅问深答</span><span class="auth-brand-caption">私有知识工作台</span></div>
      <section class="auth-intro" aria-labelledby="brand-title">
        <div class="intro-copy">
          <p class="intro-eyebrow">让知识，成为你的答案</p>
          <h1 id="brand-title">每一个好问题，<br>都值得深入解答。</h1>
          <p class="intro-description">把散落的资料整理成知识，<br>在对话中找到思路，让答案有据可循。</p>
        </div>
        <div class="intro-features">
          <div class="intro-feature"><span class="feature-icon"><AppIcon name="folder" :size="21" /></span><div><h2>按项目沉淀知识</h2><p>集中管理文档，让资料各归其位。</p></div></div>
          <div class="intro-feature"><span class="feature-icon"><AppIcon name="book" :size="21" /></span><div><h2>从提问走向理解</h2><p>结合知识库检索，追溯回答中的来源。</p></div></div>
          <div class="intro-feature"><span class="feature-icon"><AppIcon name="success" :size="21" /></span><div><h2>属于你的独立空间</h2><p>项目、文档与对话按账号独立保存。</p></div></div>
        </div>
      </section>
      <section class="auth-card" aria-labelledby="auth-title">
      <div class="auth-card-heading">
        <p class="auth-card-eyebrow">开始你的知识之旅</p>
        <h2 id="auth-title">{{ authMode === 'login' ? '欢迎回来' : '创建你的空间' }}</h2>
        <p>{{ authMode === 'login' ? '登录后，继续你的探索与对话。' : '注册账号，建立自己的私有知识库。' }}</p>
      </div>

      <div class="auth-tabs" role="group" aria-label="账号操作">
        <button type="button" :disabled="authLoading" :aria-pressed="authMode === 'login'" :class="{ active: authMode === 'login' }" @click="authMode = 'login'; authError = ''; authSuccess = ''">登录</button>
        <button type="button" :disabled="authLoading" :aria-pressed="authMode === 'register'" :class="{ active: authMode === 'register' }" @click="authMode = 'register'; authError = ''; authSuccess = ''">注册</button>
      </div>

      <AppNotice v-if="authSuccess" class="auth-notice" tone="success">{{ authSuccess }}</AppNotice>
      <AppNotice v-if="authError" class="auth-notice" tone="error">{{ authError }}</AppNotice>

      <form @submit.prevent="handleSubmit" class="form-stack auth-form" :aria-busy="authLoading">
        <div class="auth-field">
          <label for="auth-username">用户名</label>
          <input class="filter-input input-large" id="auth-username" v-model="authUsername" type="text" :placeholder="authMode === 'login' ? '请输入用户名' : '设置用户名，2–50 个字符'" autocomplete="username" :minlength="authMode === 'register' ? 2 : undefined" maxlength="50" required :disabled="authLoading" />
        </div>
        <div class="auth-field">
          <label for="auth-password">密码</label>
          <input class="filter-input input-large" id="auth-password" v-model="authPassword" type="password" :placeholder="authMode === 'login' ? '请输入密码' : '设置你的登录密码'" :autocomplete="authMode === 'login' ? 'current-password' : 'new-password'" :aria-describedby="authMode === 'register' ? 'password-hint' : undefined" :minlength="authMode === 'register' ? 10 : undefined" maxlength="128" required :disabled="authLoading" />
          <p v-if="authMode === 'register'" id="password-hint" class="field-hint">10–128 个字符，包含英文字母、数字和特殊符号。</p>
        </div>
        <div v-if="authMode === 'register'" class="auth-field">
          <label for="auth-password-confirm">确认密码</label>
          <input class="filter-input input-large" id="auth-password-confirm" v-model="authPassword2" type="password" placeholder="请再次输入密码" autocomplete="new-password" maxlength="128" required :disabled="authLoading" />
        </div>
        <button type="submit" class="btn btn-primary btn-large btn-auth" :disabled="authLoading">
          <span v-if="authLoading" class="auth-spinner" aria-hidden="true"></span>
          {{ authLoading ? '处理中…' : (authMode === 'login' ? '登录工作台' : '创建账号') }}
          <AppIcon v-if="!authLoading" name="arrow-up-right" :size="17" />
        </button>
      </form>
      <p class="auth-footer"><AppIcon name="book" :size="14" /> 你的知识与思考，在这里连接。</p>
      </section>
    </div>
  </div>
</template>

<style scoped>
.auth-page { padding: var(--page-gutter); }
.auth-layout { --auth-inset: 32px; width: min(1060px, 100%); display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(0, 1fr); align-items: stretch; column-gap: clamp(40px, 7vw, 112px); row-gap: 28px; margin: auto; }
/* 两列共用标题区域并对齐内容边缘，避免文字尺寸不同导致错位。 */
.auth-intro { display: flex; flex-direction: column; padding: calc(var(--auth-inset) + 1px) 0; }
.auth-brand { grid-column: 1 / -1; display: flex; align-items: center; gap: 11px; font-size: var(--font-heading); font-weight: 600; letter-spacing: -.4px; }
.auth-brand-caption { padding-left: 14px; border-left: 1px solid var(--line-strong); color: var(--muted); font-size: var(--font-caption); font-weight: 400; letter-spacing: 0; }
.intro-copy { margin: 0 0 36px; }
.intro-eyebrow, .auth-card-eyebrow { margin: 0 0 14px; color: var(--accent); font-size: var(--font-caption); font-weight: 600; letter-spacing: 1.2px; }
.intro-copy h1 { margin: 0; color: var(--text); font-size: clamp(32px, 3.3vw, 43px); line-height: 1.5; font-weight: 600; letter-spacing: -1.4px; }
.intro-description { margin: 21px 0 0; color: var(--muted); font-size: var(--font-reading); line-height: 1.9; }
.intro-features { display: grid; gap: 22px; margin-top: auto; padding-top: 28px; border-top: 1px solid var(--line); }
.intro-feature { display: flex; align-items: center; gap: 14px; }
.feature-icon { display: grid; place-items: center; flex-shrink: 0; width: 40px; height: 40px; border: 1px solid var(--line); border-radius: var(--radius-card); color: var(--accent); background: var(--bg); }
.intro-feature h2 { margin: 0 0 4px; font-size: var(--font-body); font-weight: 600; }
.intro-feature p { margin: 0; color: var(--muted); font-size: var(--font-caption); line-height: 1.7; }
.auth-card { width: 100%; padding: var(--auth-inset); border: 1px solid var(--line); border-radius: var(--radius-panel); background: var(--bg); box-shadow: var(--shadow-md); }
.auth-card-heading h2 { margin: 0; font-size: var(--font-title); line-height: 1.4; font-weight: 600; }
.auth-card-heading > p:last-child { margin: 10px 0 0; font-size: var(--font-small); line-height: 1.7; color: var(--muted); }
.auth-card-eyebrow { letter-spacing: .5px; margin-bottom: 10px; }
.auth-tabs { display: flex; padding: 4px; background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius-control); margin: 24px 0; }
.auth-tabs button { flex: 1; min-height: var(--control-small); padding: 6px 12px; line-height: 20px; font-size: var(--font-small); color: var(--muted); border-radius: var(--radius-small); transition: color var(--duration-fast), background var(--duration-fast); }
.auth-tabs button:hover:not(:disabled) { color: var(--accent); }
.auth-tabs button.active { color: var(--accent); background: var(--bg); font-weight: 600; box-shadow: var(--shadow-sm); }
.auth-form { gap: 20px; }
.auth-field { display: grid; gap: 8px; }
.auth-field input { width: 100%; }
.btn-auth { margin-top: 4px; }
.auth-notice { margin-bottom: 16px; }
.auth-footer { display: flex; align-items: center; justify-content: center; gap: 7px; margin: 24px 0 0; padding-top: 20px; border-top: 1px solid var(--line); color: var(--muted); font-size: var(--font-caption); }
.auth-spinner { width: 15px; height: 15px; border: 2px solid currentColor; border-right-color: transparent; border-radius: 50%; animation: spin .7s linear infinite; }
@media (max-width: 880px) {
  .auth-layout { --auth-inset: 28px; column-gap: 36px; }
  .auth-brand-caption { display: none; }
}
@media (max-width: 680px) {
  .auth-layout { display: flex; flex-direction: column; gap: 24px; max-width: 440px; }
  .auth-intro { width: 100%; padding: 0; }
  .auth-brand { justify-content: center; font-size: var(--font-heading); }
  .auth-brand-caption { display: inline; }
  .intro-copy { margin: 0; text-align: center; }
  .intro-eyebrow, .intro-description, .intro-features { display: none; }
  .intro-copy h1 { font-size: 22px; letter-spacing: -.5px; line-height: 1.7; }
  .intro-copy h1 br { display: none; }
  .auth-card { padding: 24px; }
}
@media (max-width: 380px) {
  .auth-card { padding: 24px 20px; }
  .auth-brand-caption { display: none; }
  .intro-copy h1 { font-size: 20px; }
}
</style>
