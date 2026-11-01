<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted, onUnmounted } from 'vue'
import type { Conversation, UserInfo } from '../api'
import type { AppMode } from '../types'
import BrandMark from './BrandMark.vue'
import AppDialog from './AppDialog.vue'
const props = defineProps<{
  user: UserInfo
  sidebarOpen: boolean
  collapsed: boolean
  isProcessing: boolean
  convList: Conversation[]
  currentId: string
  healthIssueCount: number
  hasMore: boolean
  loadingMore: boolean
  conversationBusy: boolean
  renameConversation: (id: string, title: string) => Promise<boolean>
}>()
const emit = defineEmits<{
  close: []
  navigate: [mode: AppMode]
  newConversation: []
  selectConversation: [id: string]
  deleteConversation: [id: string]
  loadMore: []
  logout: []
}>()
const showGearMenu = ref(false)
const conversationMenuId = ref('')
function closeConversationMenu(restoreFocus = false) {
  const trigger = sidebar.value?.querySelector<HTMLButtonElement>('.conv-more[aria-expanded="true"]')
  conversationMenuId.value = ''
  if (restoreFocus) trigger?.focus()
}
watch(() => [props.currentId, props.collapsed, props.sidebarOpen], () => closeConversationMenu())
const search = ref('')
const searchOpen = ref(false)
const searchInput = ref<HTMLInputElement | null>(null)
const searchButton = ref<HTMLButtonElement | null>(null)
async function toggleSearch() {
  searchOpen.value = !searchOpen.value
  if (searchOpen.value) {
    await nextTick()
    searchInput.value?.focus()
  } else {
    search.value = ''
    searchButton.value?.focus()
  }
}
const renameId = ref('')
const title = ref('')
const sidebar = ref<HTMLElement | null>(null)
let previousFocus: HTMLElement | null = null
const groups = computed(() => {
  const today = new Date(); today.setHours(0, 0, 0, 0)
  const result = new Map<string, Conversation[]>()
  for (const conv of props.convList.filter(c => c.title.toLocaleLowerCase().includes(search.value.trim().toLocaleLowerCase()))) {
    const time = conv.updatedAt ? new Date(conv.updatedAt).getTime() : 0
    const label = time >= +today ? '今天' : time >= +today - 86400000 ? '昨天' : time >= +today - 6 * 86400000 ? '最近七天' : '更早'
    if (!result.has(label)) result.set(label, [])
    result.get(label)!.push(conv)
  }
  return [...result].map(([label, items]) => ({ label, items }))
})
async function rename() {
  if (await props.renameConversation(renameId.value, title.value)) renameId.value = ''
}
watch(() => props.sidebarOpen, async open => {
  if (open) {
    previousFocus = document.activeElement as HTMLElement
    await nextTick()
    sidebar.value?.querySelector<HTMLButtonElement>('.mobile-close')?.focus()
  } else { previousFocus?.focus(); previousFocus = null }
})
function trapFocus(event: KeyboardEvent) {
  if (!props.sidebarOpen || event.key !== 'Tab') return
  const nodes = [...(sidebar.value?.querySelectorAll<HTMLElement>('button:not(:disabled), input') || [])].filter(el => el.getClientRects().length)
  const first = nodes[0], last = nodes[nodes.length - 1]
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
}

// 点击菜单外部时收起菜单。
function onDocumentClick(e: MouseEvent) {
  const target = e.target as HTMLElement
  if (!target.closest('.gear-wrapper')) {
    showGearMenu.value = false
  }
  if (!target.closest('.conversation-item')) closeConversationMenu()
}

function toggleGearMenu() {
  showGearMenu.value = !showGearMenu.value
}

function navigateToKnowledge() {
  showGearMenu.value = false
  emit('navigate', 'knowledge')
}
onMounted(() => document.addEventListener('click', onDocumentClick))
onUnmounted(() => document.removeEventListener('click', onDocumentClick))
</script>

<template>
  <aside ref="sidebar" class="sidebar" :class="{ open: sidebarOpen, collapsed }" aria-label="工作区导航" @keydown="trapFocus">
    <div class="sidebar-header">
      <div class="brand">
        <BrandMark />
        <span class="brand-name">浅问深答</span>
      </div>
      <button ref="searchButton" class="btn-search" :class="{ active: searchOpen }" aria-label="搜索对话" title="搜索对话" :aria-expanded="searchOpen" aria-controls="conversation-search" @click="toggleSearch">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4.5 4.5"/></svg>
      </button>
      <button class="mobile-close" aria-label="收起侧边栏" title="收起侧边栏" @click="emit('close')">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16m7-11-3 3 3 3"/></svg>
      </button>
      <button class="btn-new" :disabled="isProcessing || conversationBusy" @click="emit('newConversation')" title="开启新对话">
        <span class="plus">＋</span> 开启新对话
      </button>
    </div>

    <input v-if="searchOpen" id="conversation-search" ref="searchInput" v-model="search" class="filter-input conversation-search" placeholder="搜索已加载的对话" aria-label="搜索已加载的对话" type="search" @keydown.esc.stop="toggleSearch" />

    <ul class="conv-list">
      <li v-if="!groups.length" class="no-results">没有匹配的对话</li>
      <template v-for="group in groups" :key="group.label">
      <li class="date-group">{{ group.label }}</li>
      <li v-for="c in group.items" :key="c.id" class="conversation-item" :class="{ active: c.id === currentId, 'menu-open': conversationMenuId === c.id }" @keydown.esc.stop="closeConversationMenu(true)">
        <button class="conv-btn" :disabled="isProcessing || conversationBusy" :aria-current="c.id === currentId ? 'page' : undefined" @click="emit('selectConversation', c.id)">

          <span class="conv-title">{{ c.title }}</span>
        </button>
        <button class="conv-more" :disabled="isProcessing || conversationBusy" :aria-expanded="conversationMenuId === c.id" :aria-controls="`conversation-actions-${c.id}`" :aria-label="`${c.title}的更多操作`" title="更多操作" @click="conversationMenuId = conversationMenuId === c.id ? '' : c.id">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="5" cy="12" r="1.8"/><circle cx="12" cy="12" r="1.8"/><circle cx="19" cy="12" r="1.8"/></svg>
        </button>
        <div v-if="conversationMenuId === c.id" :id="`conversation-actions-${c.id}`" class="conversation-menu" role="group" aria-label="对话操作">
          <button :disabled="isProcessing || conversationBusy" @click="closeConversationMenu(true); renameId = c.id; title = c.title">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m16 3 5 5-12 12-6 1 1-6L16 3Zm-2 2 5 5"/></svg>重命名
          </button>
          <button class="delete-action" :disabled="isProcessing || conversationBusy" @click="closeConversationMenu(true); emit('deleteConversation', c.id)">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"/></svg>删除
          </button>
        </div>
      </li>
      </template>
      <li v-if="hasMore" class="conversation-pagination">
        <button class="load-more-button" :disabled="loadingMore" :aria-busy="loadingMore" @click="emit('loadMore')">
          <svg v-if="!loadingMore" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6" /></svg>
          {{ loadingMore ? '正在加载…' : '加载更多对话' }}
        </button>
      </li>
    </ul>

    <!-- 左下角：用户标识 + 齿轮菜单 -->
    <div class="sidebar-footer">
      <div class="divider"></div>
      <div class="user-area">
        <div class="user-info">
          <span class="user-avatar">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none"><path d="M12 12C14.7614 12 17 9.76142 17 7C17 4.23858 14.7614 2 12 2C9.23858 2 7 4.23858 7 7C7 9.76142 9.23858 12 12 12Z" fill="currentColor"/><path d="M12 14C7.58172 14 4 17.5817 4 22H20C20 17.5817 16.4183 14 12 14Z" fill="currentColor"/></svg>
          </span>
          <span class="user-name">{{ user.username }}</span>
          <span class="user-role" :class="user.role">{{ user.role === 'admin' ? '管理员' : '用户' }}</span>
        </div>
        <div class="gear-wrapper">
          <button class="btn-gear" @click="toggleGearMenu" title="设置">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.66a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
            <span v-if="healthIssueCount > 0" class="badge">{{ healthIssueCount }}</span>
          </button>
          <div v-if="showGearMenu" class="gear-menu">
            <button v-if="user.role === 'admin'" :disabled="isProcessing" @click="navigateToKnowledge">
              管理知识库
              <span v-if="healthIssueCount > 0" class="badge menu-badge">{{ healthIssueCount }}</span>
            </button>
            <button class="logout-item" @click="emit('logout')">退出登录</button>
          </div>
        </div>
      </div>
    </div>
  </aside>
  <AppDialog v-if="renameId" title="重命名对话" compact @close="renameId = ''">
    <form class="rename-form" @submit.prevent="rename">
      <input v-model="title" class="filter-input" aria-label="对话名称" maxlength="255" autofocus />
      <button :disabled="!title.trim() || conversationBusy" type="submit">{{ conversationBusy ? '保存中…' : '保存' }}</button>
    </form>
  </AppDialog>
</template>

<style scoped>
.conversation-search { margin: 0 10px 12px; width: calc(100% - 20px); }
.conv-list .date-group, .conv-list .no-results { padding: 10px 12px; font-size: 11px; color: var(--muted); background: transparent; }
.rename-form { display: flex; flex-direction: column; gap: 16px; }
.rename-form input { width: 100%; min-width: 0; }
.rename-form button { align-self: flex-end; padding: 8px 20px; border-radius: 7px; background: var(--accent); color: white; }
.sidebar { width: 256px; flex-shrink: 0; padding: 24px 12px 12px; background: var(--sidebar); display: flex; flex-direction: column; border-right: 1px solid var(--line); }
.sidebar-header { display: flex; align-items: center; gap: 8px; padding: 0 8px 24px; flex-wrap: wrap; }
.brand { display: flex; gap: 9px; align-items: center; flex: 1; white-space: nowrap; }
.brand-name { font-weight: 650; font-size: 16px; letter-spacing: -.3px; }
.btn-search { width: 30px; height: 30px; flex-shrink: 0; display: grid; place-items: center; border-radius: 6px; color: var(--muted); }
.btn-search:hover, .btn-search.active { background: var(--hover); color: var(--text); }
.btn-new { display: flex; align-items: center; gap: 9px; width: 100%; margin-top: 20px; padding: 10px 12px; background: var(--bg); border: 1px solid #dedede; border-radius: 9px; text-align: left; font-size: 13px; box-shadow: 0 1px 2px #00000004; }
.plus { font-size: 19px; line-height: 1; font-weight: 400; }
.btn-new:hover:not(:disabled) { background: var(--hover); }
.conv-list { list-style: none; margin: 0; padding: 0; flex: 1; overflow-y: auto; min-height: 0; }
.conv-list li { display: flex; align-items: center; border-radius: 7px; margin-bottom: 3px; }
.conv-list li:hover, .conv-list li.active { background: var(--hover); }
.conv-btn { flex: 1; min-width: 0; padding: 10px 12px; text-align: left; font-size: 13px; }
.conv-list .conversation-pagination { justify-content: center; margin: 12px 8px 4px; padding-top: 12px; border-top: 1px solid var(--line); border-radius: 0; background: transparent; }
.load-more-button { display: inline-flex; align-items: center; justify-content: center; gap: 6px; padding: 7px 12px; border: 1px solid var(--line); border-radius: 16px; color: var(--muted); font-size: 12px; }
.load-more-button:hover:not(:disabled) { background: var(--hover); color: var(--text); }
.load-more-button:disabled { opacity: .6; cursor: wait; }
.conv-title { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.conversation-item { flex-wrap: wrap; }
.conv-more { width: 30px; height: 30px; flex-shrink: 0; display: grid; place-items: center; opacity: 0; color: var(--muted); border-radius: 5px; }
.conversation-item:hover .conv-more, .conversation-item:focus-within .conv-more, .conversation-item.active .conv-more, .conversation-item.menu-open .conv-more { opacity: 1; }
.conv-more:hover { color: var(--text); background: var(--bg); }
.conversation-menu { flex-basis: 100%; margin: 0 6px 6px; padding: 4px; border: 1px solid var(--line); border-radius: 8px; background: var(--bg); box-shadow: 0 4px 12px #00000008; }
.conversation-menu button { display: flex; align-items: center; gap: 9px; width: 100%; padding: 8px; border-radius: 5px; text-align: left; font-size: 12px; }
.conversation-menu button:hover:not(:disabled) { background: var(--hover); }
.conversation-menu .delete-action { color: var(--danger); }
.sidebar-footer { margin-top: auto; padding: 12px 5px 0; }
.divider { height: 1px; background: var(--line); margin-bottom: 14px; }
.user-area, .user-info { display: flex; align-items: center; gap: 9px; min-width: 0; }
.user-info { flex: 1; }
.user-avatar { width: 30px; height: 30px; background: #e5e5e7; color: #646466; border-radius: 50%; display: grid; place-items: center; flex-shrink: 0; }
.user-name { font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.user-role { font-size: 10px; color: var(--muted); white-space: nowrap; }
.gear-wrapper { position: relative; }
.btn-gear { width: 32px; height: 32px; display: grid; place-items: center; border-radius: 7px; position: relative; color: var(--muted); }
.btn-gear:hover { background: var(--hover); }
.badge { background: var(--danger); color: white; border-radius: 20px; font-size: 10px; padding: 1px 4px; position: absolute; top: -3px; right: -3px; }
.menu-badge { position: static; margin-left: 8px; }
.gear-menu { position: absolute; bottom: 42px; right: 0; width: 180px; background: white; border: 1px solid var(--line); border-radius: 10px; padding: 5px; box-shadow: 0 8px 28px #00000014; z-index: 30; }
.gear-menu button { display: block; padding: 10px; font-size: 13px; width: 100%; text-align: left; border-radius: 5px; }
.gear-menu button:hover { background: var(--surface); }
.logout-item { color: var(--danger); }
.mobile-close { display: grid; place-items: center; width: 30px; height: 30px; border-radius: 6px; color: var(--muted); flex-shrink: 0; }
.mobile-close:hover { background: var(--hover); color: var(--text); }
@media (min-width: 761px) {
  .sidebar.collapsed { display: none; }
}
@media (max-width: 760px) {
  .sidebar { position: fixed; inset: 0 auto 0 0; width: 272px; z-index: 50; visibility: hidden; transform: translateX(-100%); transition: transform .2s, visibility .2s; }
  .sidebar.open { visibility: visible; transform: translateX(0); }
}
</style>
