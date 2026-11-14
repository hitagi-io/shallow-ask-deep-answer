<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { apiDeleteAccount, apiUpdateAccount, apiVerifyDeletion, type UserInfo } from '../api'
import SettingsPage from '../components/layout/SettingsPage.vue'
import AppDialog from '../components/common/AppDialog.vue'
import AppNotice from '../components/common/AppNotice.vue'

const props = defineProps<{ user: UserInfo }>()
const emit = defineEmits<{ back: []; updated: [user: UserInfo]; deleted: []; passwordChanged: [] }>()
const username = ref(props.user.username)
watch(() => props.user.username, value => { username.value = value })
const currentPassword = ref('')
const newPassword = ref('')
const repeatPassword = ref('')
const saving = ref<'name' | 'password' | 'verify' | 'delete' | null>(null)
const busy = computed(() => saving.value !== null)
const nameError = ref('')
const nameSuccess = ref('')
const passwordError = ref('')
const deleteStep = ref<0 | 1 | 2 | 3>(0)
const acknowledged = ref(false)
const deletePassword = ref('')
const deleteToken = ref('')
const requiredText = ref('')
const confirmationText = ref('')
const deleteError = ref('')
const stepContent = ref<HTMLElement | null>(null)

async function saveName() {
  if (busy.value) return
  nameError.value = ''; nameSuccess.value = ''
  if (username.value.trim().length < 2) { nameError.value = '用户名需为 2～50 个字符'; return }
  saving.value = 'name'
  try {
    const user = await apiUpdateAccount({ username: username.value.trim() })
    emit('updated', user)
    nameSuccess.value = '名字已更新，下次请使用新用户名登录。'
  } catch (error) { nameError.value = (error as Error).message }
  finally { saving.value = null }
}

async function savePassword() {
  if (busy.value) return
  passwordError.value = ''
  if (newPassword.value !== repeatPassword.value) { passwordError.value = '两次输入的新密码不一致'; return }
  if (newPassword.value.length < 10 || !/[0-9]/.test(newPassword.value) || !/[a-zA-Z]/.test(newPassword.value) || !/[^0-9a-zA-Z\s]/.test(newPassword.value)) {
    passwordError.value = '新密码需为 10～128 个字符，包含英文字母、数字和特殊符号。'; return
  }
  saving.value = 'password'
  try {
    await apiUpdateAccount({ current_password: currentPassword.value, new_password: newPassword.value })
    currentPassword.value = ''; newPassword.value = ''; repeatPassword.value = ''
    emit('passwordChanged')
  } catch (error) { passwordError.value = (error as Error).message }
  finally { saving.value = null }
}

function closeDeletion() {
  if (busy.value) return
  deleteStep.value = 0
  acknowledged.value = false; deletePassword.value = ''; deleteToken.value = ''
  requiredText.value = ''; confirmationText.value = ''; deleteError.value = ''
}
async function moveStep(step: 1 | 2 | 3) {
  deleteError.value = ''
  deleteStep.value = step
  await nextTick()
  stepContent.value?.querySelector<HTMLElement>('input, button')?.focus()
}
function reverify() {
  deleteToken.value = ''; confirmationText.value = ''; requiredText.value = ''
  void moveStep(2)
}
async function verifyDeletion() {
  if (busy.value || !acknowledged.value || deleteStep.value !== 2) return
  saving.value = 'verify'; deleteError.value = ''
  try {
    const result = await apiVerifyDeletion(deletePassword.value)
    deleteToken.value = result.confirmation_token
    requiredText.value = result.confirmation_text
    deletePassword.value = ''
    saving.value = null
    await moveStep(3)
  } catch (error) { deleteError.value = (error as Error).message }
  finally { saving.value = null }
}
async function confirmDeletion() {
  if (busy.value || deleteStep.value !== 3 || !deleteToken.value || confirmationText.value !== requiredText.value) return
  saving.value = 'delete'; deleteError.value = ''
  try {
    await apiDeleteAccount(deleteToken.value, confirmationText.value)
    emit('deleted')
  } catch (error) { deleteError.value = (error as Error).message }
  finally { saving.value = null }
}
</script>

<template>
  <SettingsPage title="编辑信息" description="管理你的名字、登录密码和账号。" :busy="busy" @back="emit('back')">
    <div class="account-summary"><span class="account-avatar" aria-hidden="true">{{ user.username.slice(0, 1).toLocaleUpperCase() }}</span><div><h2>{{ user.username }}</h2><p>我的私有知识空间</p></div></div>
    <section class="account-panel" aria-labelledby="name-title">
      <div class="panel-description"><h2 id="name-title">名字</h2><p>这个名字也用于登录。修改后，你的项目和对话仍会保留。</p></div>
      <form class="form-stack" @submit.prevent="saveName" :aria-busy="saving === 'name'">
        <AppNotice v-if="nameError" tone="error">{{ nameError }}</AppNotice>
        <AppNotice v-if="nameSuccess" tone="success">{{ nameSuccess }}</AppNotice>
        <label for="profile-name">用户名</label>
        <input id="profile-name" v-model="username" class="filter-input" autocomplete="username" minlength="2" maxlength="50" required :disabled="busy" aria-describedby="name-hint" />
        <p id="name-hint" class="field-hint">2～50 个字符，不可与其他账号重名。</p>
        <button class="btn btn-primary" :disabled="busy || username.trim() === user.username" type="submit">{{ saving === 'name' ? '保存中…' : '保存名字' }}</button>
      </form>
    </section>
    <section class="account-panel" aria-labelledby="password-title">
      <div class="panel-description"><h2 id="password-title">登录密码</h2><p>先验证当前密码，再设置新密码。修改后，包括当前页面在内的所有登录会话都需要使用新密码重新登录。</p></div>
      <form class="form-stack" @submit.prevent="savePassword" :aria-busy="saving === 'password'">
        <AppNotice v-if="passwordError" tone="error">{{ passwordError }}</AppNotice>
        <label for="current-password">当前密码</label>
        <input id="current-password" v-model="currentPassword" class="filter-input" type="password" autocomplete="current-password" maxlength="128" required :disabled="busy" />
        <label for="new-password">新密码</label>
        <input id="new-password" v-model="newPassword" class="filter-input" type="password" autocomplete="new-password" minlength="10" maxlength="128" required :disabled="busy" aria-describedby="new-password-hint" />
        <p id="new-password-hint" class="field-hint">10～128 个字符，包含英文字母、数字和特殊符号。</p>
        <label for="repeat-password">再次输入新密码</label>
        <input id="repeat-password" v-model="repeatPassword" class="filter-input" type="password" autocomplete="new-password" minlength="10" maxlength="128" required :disabled="busy" />
        <button class="btn btn-primary" :disabled="busy" type="submit">{{ saving === 'password' ? '更新中…' : '更新密码' }}</button>
      </form>
    </section>
    <section class="account-panel danger-panel" aria-labelledby="delete-title">
      <div class="panel-description"><h2 id="delete-title">注销账号</h2><p>完成多步骤确认后立即删除账号及其数据。此操作无法撤销。</p></div>
      <div class="deletion-entry"><p>账号下的项目、上传文档、知识索引和全部对话都会被删除。建议先自行保存需要保留的内容。操作审计保留编号、操作类型及结果，90 天后自动删除。</p><button class="btn btn-secondary btn-danger" :disabled="busy" @click="moveStep(1)">申请注销账号</button></div>
    </section>
  </SettingsPage>

  <AppDialog v-if="deleteStep" title="注销账号" compact @close="closeDeletion">
    <ol class="deletion-steps" aria-label="注销确认步骤"><li v-for="(label, index) in ['阅读须知', '验证身份', '最终确认']" :key="label" :aria-current="deleteStep === index + 1 ? 'step' : undefined">{{ index + 1 }} · {{ label }}</li></ol>
    <div ref="stepContent" :aria-busy="busy">
      <AppNotice v-if="deleteError" tone="error" class="delete-notice">{{ deleteError }}</AppNotice>
      <div v-if="deleteStep === 1" class="delete-copy">
        <h3>确认要离开这个空间吗？</h3>
        <p>最后一步确认后，将立即删除：</p>
        <ul><li>你的账号和登录信息</li><li>所有项目、上传文档和知识索引</li><li>全部对话记录及模型上下文</li></ul>
        <p>无法撤销或恢复。有回答正在生成时，请先停止或等待完成。</p>
        <label class="acknowledgement"><input v-model="acknowledged" type="checkbox" /><span>我已了解删除范围，并已保存需要保留的内容。</span></label>
        <div class="dialog-actions"><button class="btn btn-secondary" autofocus @click="closeDeletion">保留账号</button><button class="btn btn-primary" :disabled="!acknowledged" @click="moveStep(2)">下一步</button></div>
      </div>
      <form v-else-if="deleteStep === 2" class="form-stack delete-form" @submit.prevent="verifyDeletion">
        <p>请输入 <strong>{{ user.username }}</strong> 的当前密码以验证身份。此步骤不会删除数据。</p>
        <label for="delete-password">当前密码</label>
        <input id="delete-password" v-model="deletePassword" class="filter-input" type="password" autocomplete="current-password" maxlength="128" required :disabled="busy" />
        <div class="dialog-actions"><button type="button" class="btn btn-secondary" :disabled="busy" @click="closeDeletion">取消</button><button type="submit" class="btn btn-primary" :disabled="busy">{{ saving === 'verify' ? '验证中…' : '验证并继续' }}</button></div>
      </form>
      <form v-else class="form-stack delete-form" @submit.prevent="confirmDeletion">
        <AppNotice tone="warning">这是最后一步。点击下方删除按钮后，账号及数据将立即删除，无法恢复。</AppNotice>
        <label for="delete-confirmation">请输入「{{ requiredText }}」</label>
        <input id="delete-confirmation" v-model="confirmationText" class="filter-input" autocomplete="off" spellcheck="false" maxlength="60" required :disabled="busy" aria-describedby="delete-hint" />
        <p id="delete-hint" class="field-hint">身份验证 5 分钟内有效。过期后请重新验证。</p>
        <button type="button" class="text-action" :disabled="busy" @click="reverify">重新验证身份</button>
        <p v-if="saving === 'delete'" role="status">正在清理账号数据，请等待结果…</p>
        <div class="dialog-actions"><button type="button" class="btn btn-secondary" :disabled="busy" @click="closeDeletion">保留账号</button><button type="submit" class="btn delete-submit" :disabled="busy || !deleteToken || confirmationText !== requiredText">{{ saving === 'delete' ? '注销中…' : '永久删除账号及数据' }}</button></div>
      </form>
    </div>
  </AppDialog>
</template>

<style scoped>
.account-summary { display: flex; align-items: center; gap: 16px; margin-bottom: var(--section-gap); }
.account-avatar { display: grid; place-items: center; width: 56px; height: 56px; flex-shrink: 0; border: 1px solid var(--accent-border); border-radius: var(--radius-panel); background: var(--accent-soft); font-size: 22px; font-weight: 600; }
.account-summary h2 { margin: 0 0 4px; font-size: var(--font-heading); overflow-wrap: anywhere; }
.account-summary p { margin: 0; font-size: var(--font-caption); color: var(--muted); }
.account-panel { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.35fr); gap: 32px; padding: var(--panel-padding); margin-bottom: var(--section-gap); border: 1px solid var(--line); border-radius: var(--radius-card); background: var(--bg); }
.panel-description h2 { margin: 0 0 10px; font-size: var(--font-subheading); font-weight: 600; }
.panel-description p, .deletion-entry p { margin: 0; color: var(--muted); font-size: var(--font-small); line-height: 1.85; }
input.filter-input { width: 100%; }
form > label:not(:first-child) { margin-top: 4px; }
form > .btn { align-self: flex-start; margin-top: 10px; }
.danger-panel { border-color: var(--danger-border); }
.danger-panel h2 { color: var(--danger); }
.deletion-entry .btn { margin-top: 18px; }
.deletion-steps { display: flex; justify-content: space-between; list-style: none; padding: 0; margin: 0 0 24px; gap: 10px; color: var(--muted); font-size: var(--font-caption); }
.deletion-steps li[aria-current] { color: var(--text); font-weight: 600; }
.delete-copy, .delete-form { font-size: var(--font-small); line-height: 1.85; overflow-wrap: anywhere; }
.delete-copy h3 { margin: 0 0 12px; font-size: var(--font-heading); font-weight: 600; }
.delete-copy ul { padding-left: 20px; }
.delete-form p { margin: 0 0 8px; }
.acknowledgement { display: flex; align-items: flex-start; gap: 9px; padding: 12px; background: var(--surface); border-radius: var(--radius-control); font-weight: 400; }
.acknowledgement input { margin-top: 6px; flex-shrink: 0; }
.delete-submit { background: var(--danger); color: var(--on-accent); }
.delete-submit:hover:not(:disabled) { filter: brightness(.9); }
.delete-notice { margin-bottom: 16px; }
.delete-form .text-action { align-self: flex-start; }
@media (max-width: 760px) { .account-panel { grid-template-columns: 1fr; gap: 24px; } }
</style>
