<script setup lang="ts">
import { computed, ref, watch, nextTick, onMounted, onUnmounted } from 'vue'
import type { UserInfo } from '../api'
import type { AppMode } from '../types'
import { useChat } from '../composables/useChat'
import { useKnowledge } from '../composables/useKnowledge'
import WorkspaceSidebar from './WorkspaceSidebar.vue'
import ChatView from './ChatView.vue'
import KnowledgeView from './KnowledgeView.vue'
import AppDialog from './AppDialog.vue'
import { useConfirmation } from '../composables/useConfirmation'

const props = defineProps<{ user: UserInfo }>()
const emit = defineEmits<{ logout: [] }>()
const MODE_KEY = 'deepagent_mode'
const isAdmin = computed(() => props.user.role === 'admin')
const mode = ref<AppMode>(isAdmin.value && localStorage.getItem(MODE_KEY) === 'knowledge' ? 'knowledge' : 'chat')
watch(mode, value => localStorage.setItem(MODE_KEY, value), { immediate: true })
const sidebarOpen = ref(false)
const sidebarCollapsed = ref(false)
const sidebarToggle = ref<HTMLButtonElement | null>(null)
const confirmation = useConfirmation()
const { message: confirmationMessage } = confirmation
const chat = useChat(confirmation.ask)
const { currentId, convList, processingIds } = chat
const knowledge = useKnowledge(isAdmin, computed(() => mode.value === 'knowledge'), confirmation.ask)
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
      <ChatView v-if="mode === 'chat'" :chat="chat" />
      <KnowledgeView v-else :knowledge="knowledge" @back="navigate('chat')" />
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
.layout { display: flex; height: 100vh; height: 100dvh; }
.main { position: relative; flex: 1; min-width: 0; display: flex; flex-direction: column; }
.mobile-menu, .sidebar-backdrop { display: none; }
.mobile-menu { position: absolute; top: 8px; left: 8px; z-index: 10; place-items: center; width: 30px; height: 30px; border: 1px solid var(--line); border-radius: 6px; background: var(--bg); color: var(--muted); }
.mobile-menu.desktop-visible { display: grid; }
.mobile-menu:hover { background: var(--hover); }
@media (max-width: 760px) {
  .sidebar-backdrop { display: block; position: fixed; inset: 0; background: #0005; z-index: 45; }
  .mobile-menu { display: grid; place-items: center; width: 30px; height: 30px; border-radius: 6px; font-size: 19px; flex-shrink: 0; }
  .mobile-menu:hover { background: var(--hover); }
}
</style>
