<script setup lang="ts">
import { computed, ref, watch, nextTick, onMounted, onUnmounted } from 'vue'
import type { UserInfo } from '../api'
import type { AppMode } from '../types'
import { useChat } from '../composables/useChat'
import { useKnowledge } from '../composables/useKnowledge'
import { useProjects } from '../composables/useProjects'
import WorkspaceSidebar from './WorkspaceSidebar.vue'
import ChatView from './ChatView.vue'
import KnowledgeView from './KnowledgeView.vue'
import AccountView from './AccountView.vue'
import AboutView from './AboutView.vue'
import AppDialog from './AppDialog.vue'
import { useConfirmation } from '../composables/useConfirmation'

const props = defineProps<{ user: UserInfo }>()
const emit = defineEmits<{ logout: []; userUpdated: [user: UserInfo]; accountDeleted: []; passwordChanged: [] }>()
const MODE_KEY = `shallow_ask_deep_answer_mode_${props.user.id}`
// 仅刷新时恢复页面；新进入或恢复标签页时默认打开问答首页。
const navigation = performance.getEntriesByType('navigation')[0] as PerformanceNavigationTiming | undefined
const savedMode = sessionStorage.getItem(MODE_KEY)
const mode = ref<AppMode>(navigation?.type === 'reload' && ['knowledge', 'account', 'about'].includes(savedMode || '') ? savedMode as AppMode : 'chat')
localStorage.removeItem(MODE_KEY)
watch(mode, value => sessionStorage.setItem(MODE_KEY, value), { immediate: true })
const sidebarOpen = ref(false)
const sidebarCollapsed = ref(false)
const sidebarToggle = ref<HTMLButtonElement | null>(null)
const confirmation = useConfirmation()
const { message: confirmationMessage } = confirmation
const projects = useProjects(confirmation.ask)
const chat = useChat(projects.projects, confirmation.ask)
const { currentId, convList, processingIds } = chat
const knowledge = useKnowledge(projects.selectedId, computed(() => mode.value === 'knowledge'), confirmation.ask)
const { healthIssueCount } = knowledge
const desktop = window.matchMedia('(min-width: 761px)')
async function closeSidebar() {
  if (desktop.matches) {
    sidebarCollapsed.value = true
    await nextTick()
    sidebarToggle.value?.focus()
  } else sidebarOpen.value = false
}
function openSidebar() {
  if (desktop.matches) sidebarCollapsed.value = false
  else sidebarOpen.value = true
}
function closeDesktopDrawer() { if (desktop.matches) sidebarOpen.value = false }
onMounted(() => desktop.addEventListener('change', closeDesktopDrawer))
onUnmounted(() => desktop.removeEventListener('change', closeDesktopDrawer))

function navigate(value: AppMode) {
  mode.value = value
  sidebarOpen.value = false
  if (value === 'knowledge') {
    if (!knowledge.knowledgeBusy.value && chat.selectedProject.value) projects.selectedId.value = chat.selectedProject.value.id
    void projects.load()
  }
}
async function askProject(id: string) {
  await chat.newConversation(true, id)
  navigate('chat')
}
function newConversation() {
  navigate('chat')
  chat.newConversation()
}
function selectConversation(id: string) {
  navigate('chat')
  chat.selectConversation(id)
}
async function logout() {
  if (await confirmation.ask('确定要退出登录吗？')) emit('logout')
}
</script>

<template>
  <div class="layout" @keydown.esc="sidebarOpen = false">
    <button v-if="mode === 'chat' && sidebarOpen" class="sidebar-backdrop" aria-label="关闭导航" @click="sidebarOpen = false"></button>
    <WorkspaceSidebar v-if="mode === 'chat'" :user="user" :sidebar-open="sidebarOpen" :collapsed="sidebarCollapsed"
      :conversation-busy="chat.conversationBusy.value || chat.loading.value" :rename-conversation="chat.renameConversation"
      :has-more="!!chat.nextCursor.value" :loading-more="chat.loadingMore.value" @load-more="chat.loadMoreConversations"
      :processing-ids="processingIds" :conv-list="convList"
      :current-id="currentId" :health-issue-count="healthIssueCount"
      @close="closeSidebar" @navigate="navigate" @new-conversation="newConversation"
      @select-conversation="selectConversation" @delete-conversation="chat.deleteConversation" @logout="logout" />
    <main class="main" :inert="mode === 'chat' && sidebarOpen">
        <button v-if="mode === 'chat'" ref="sidebarToggle" class="mobile-menu" :class="{ 'desktop-visible': sidebarCollapsed }" aria-label="展开侧边栏" title="展开侧边栏" @click="openSidebar">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16m4-11 3 3-3 3"/></svg>
        </button>
      <ChatView v-if="mode === 'chat'" :chat="chat" :projects="projects" @manage="navigate('knowledge')" />
      <KnowledgeView v-else-if="mode === 'knowledge'" :knowledge="knowledge" :projects="projects" @back="navigate('chat')" @ask="askProject" @deleted="chat.removeProjectConversations" />
      <AccountView v-else-if="mode === 'account'" :user="user" @back="navigate('chat')" @updated="emit('userUpdated', $event)" @deleted="emit('accountDeleted')" @password-changed="emit('passwordChanged')" />
      <AboutView v-else @back="navigate('chat')" @knowledge="navigate('knowledge')" />
    </main>
  </div>
  <AppDialog v-if="confirmationMessage" title="确认操作" compact @close="confirmation.settle(false)">
    <p class="confirmation-copy">{{ confirmationMessage }}</p>
    <div class="confirmation-actions"><button class="btn btn-secondary" autofocus @click="confirmation.settle(false)">取消</button><button class="btn btn-primary" @click="confirmation.settle(true)">确认</button></div>
  </AppDialog>
</template>

<style scoped>
.confirmation-copy { font-size: 14px; line-height: 1.8; overflow-wrap: anywhere; }
.confirmation-actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 20px; }
.layout { display: flex; height: 100vh; height: 100dvh; background: var(--canvas); }
.main { position: relative; flex: 1; min-width: 0; display: flex; flex-direction: column; background: var(--bg); }
.mobile-menu, .sidebar-backdrop { display: none; }
.mobile-menu { position: absolute; top: 18px; left: 20px; z-index: 10; place-items: center; width: 36px; height: 36px; border: 1px solid var(--line); border-radius: var(--radius-control); background: var(--bg); color: var(--muted); transition: background-color var(--duration-fast); }
.mobile-menu.desktop-visible { display: grid; }
.mobile-menu:hover { background: var(--hover); }
@media (max-width: 760px) {
  .sidebar-backdrop { display: block; position: fixed; inset: 0; background: var(--overlay); backdrop-filter: blur(3px); z-index: 45; }
  .mobile-menu { display: grid; place-items: center; left: 16px; flex-shrink: 0; }
  .mobile-menu:hover { background: var(--hover); }
}
</style>
