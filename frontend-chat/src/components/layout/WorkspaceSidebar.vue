<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted, onUnmounted } from 'vue'
import type { Conversation, UserInfo } from '../../api'
import type { AppMode } from '../../types'
import BrandMark from '../common/BrandMark.vue'
import AppDialog from '../common/AppDialog.vue'
import AppIcon from '../common/AppIcon.vue'
const props = defineProps<{
  user: UserInfo
  sidebarOpen: boolean
  collapsed: boolean
  processingIds: string[]
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
const gearButton = ref<HTMLButtonElement | null>(null)
function closeGearMenu() {
  showGearMenu.value = false
  gearButton.value?.focus()
}
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
watch(conversationMenuId, async id => {
  if (!id) return
  await nextTick()
  sidebar.value?.querySelector<HTMLElement>('.conversation-menu')?.scrollIntoView({ block: 'nearest', inline: 'nearest' })
})
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
  navigateTo('knowledge')
}
function navigateTo(mode: AppMode) {
  showGearMenu.value = false
  emit('navigate', mode)
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
      <button ref="searchButton" class="icon-button btn-search" :class="{ active: searchOpen }" aria-label="搜索对话" title="搜索对话" :aria-expanded="searchOpen" aria-controls="conversation-search" @click="toggleSearch">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4.5 4.5"/></svg>
      </button>
      <button class="icon-button mobile-close" aria-label="收起侧边栏" title="收起侧边栏" @click="emit('close')">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16m7-11-3 3 3 3"/></svg>
      </button>
      <button class="btn btn-primary btn-new" :disabled="conversationBusy" @click="emit('newConversation')" title="开启新对话">
        <AppIcon name="plus" /> 开启新对话
      </button>
    </div>

    <input v-if="searchOpen" id="conversation-search" ref="searchInput" v-model="search" class="filter-input conversation-search" placeholder="搜索已加载的对话" aria-label="搜索已加载的对话" type="search" @keydown.esc.stop="toggleSearch" />

    <nav class="workspace-nav" aria-label="工作空间">
      <button class="knowledge-entry" @click="navigateToKnowledge"><AppIcon name="book" :size="18" /><span>知识库</span><span v-if="healthIssueCount > 0" class="nav-badge">{{ healthIssueCount }}</span><AppIcon v-else name="arrow-up-right" :size="15" /></button>
    </nav>
    <div class="history-label"><span>最近对话</span><span class="history-count">{{ convList.length }}{{ hasMore ? '+' : '' }}</span></div>
    <ul class="conv-list" aria-label="对话历史">
      <li v-if="!groups.length" class="no-results">{{ search ? '没有找到匹配的对话' : '从一个问题开始，记录会保存在这里' }}</li>
      <template v-for="group in groups" :key="group.label">
      <li class="date-group">{{ group.label }}</li>
      <li v-for="c in group.items" :key="c.id" class="conversation-item" :class="{ active: c.id === currentId, 'menu-open': conversationMenuId === c.id }" @keydown.esc.stop="closeConversationMenu(true)">
        <button class="conv-btn" :disabled="conversationBusy" :aria-current="c.id === currentId ? 'page' : undefined" @click="emit('selectConversation', c.id)">

          <span class="conv-title">{{ c.title }}</span>
          <span v-if="c.project_deleted" class="deleted-project-badge">项目已删除</span>
          <span v-if="processingIds.includes(c.id)" class="conversation-progress" role="status" aria-label="正在生成回答"></span>
        </button>
        <button class="icon-button conv-more" :disabled="conversationBusy" :aria-expanded="conversationMenuId === c.id" :aria-controls="`conversation-actions-${c.id}`" :aria-label="`${c.title}的更多操作`" title="更多操作" @click="conversationMenuId = conversationMenuId === c.id ? '' : c.id">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="5" cy="12" r="1.8"/><circle cx="12" cy="12" r="1.8"/><circle cx="19" cy="12" r="1.8"/></svg>
        </button>
        <div v-if="conversationMenuId === c.id" :id="`conversation-actions-${c.id}`" class="menu-panel conversation-menu" role="group" aria-label="对话操作">
          <button :disabled="conversationBusy" @click="closeConversationMenu(true); renameId = c.id; title = c.title">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m16 3 5 5-12 12-6 1 1-6L16 3Zm-2 2 5 5"/></svg>重命名
          </button>
          <button class="btn-danger" :disabled="conversationBusy || processingIds.includes(c.id)" @click="closeConversationMenu(true); emit('deleteConversation', c.id)">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"/></svg>删除
          </button>
        </div>
      </li>
      </template>
      <li v-if="hasMore" class="conversation-pagination">
        <button class="btn btn-secondary btn-small load-more-button" :disabled="loadingMore" :aria-busy="loadingMore" @click="emit('loadMore')">
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
          <span class="user-avatar" aria-hidden="true">{{ user.username.slice(0, 1).toLocaleUpperCase() }}</span>
          <div class="user-details"><span class="user-name">{{ user.username }}</span><span class="user-space">我的私有空间</span></div>
        </div>
        <div class="gear-wrapper" @keydown.esc.stop="closeGearMenu">
          <button ref="gearButton" class="icon-button btn-gear" @click="toggleGearMenu" title="账号设置" aria-label="账号设置" :aria-expanded="showGearMenu" aria-controls="account-menu">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.66a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
            <span v-if="healthIssueCount > 0" class="badge">{{ healthIssueCount }}</span>
          </button>
          <div v-if="showGearMenu" id="account-menu" class="menu-panel gear-menu" role="group" aria-label="账号操作">
            <button @click="navigateToKnowledge">
              管理知识库
              <span v-if="healthIssueCount > 0" class="badge menu-badge">{{ healthIssueCount }}</span>
            </button>
            <button @click="navigateTo('account')">编辑信息</button>
            <button @click="navigateTo('about')">关于</button>
            <button class="btn-danger" @click="showGearMenu = false; emit('logout')">退出登录</button>
          </div>
        </div>
      </div>
    </div>
  </aside>
  <AppDialog v-if="renameId" title="重命名对话" compact @close="renameId = ''">
    <form class="form-stack rename-form" @submit.prevent="rename">
      <input v-model="title" class="filter-input" aria-label="对话名称" maxlength="255" autofocus />
      <button class="btn btn-primary" :disabled="!title.trim() || conversationBusy" type="submit">{{ conversationBusy ? '保存中…' : '保存' }}</button>
    </form>
  </AppDialog>
</template>

<style scoped>
.sidebar { width: 276px; flex-shrink: 0; padding: 24px 16px 16px; background: var(--sidebar); display: flex; flex-direction: column; border-right: 1px solid var(--line); }
.sidebar-header { display: flex; align-items: center; gap: 4px; padding: 0 4px 16px; flex-wrap: wrap; }
.brand { display: flex; gap: 8px; align-items: center; flex: 1; white-space: nowrap; }
.brand-name { font-weight: 600; font-size: var(--font-heading); letter-spacing: -.4px; }
.btn-new { width: 100%; margin-top: 24px; }
.workspace-nav { padding: 0 4px 20px; margin-bottom: 16px; border-bottom: 1px solid var(--line); }
.knowledge-entry { display: flex; align-items: center; gap: 10px; width: 100%; min-height: var(--control-height); padding: 8px 10px; border-radius: var(--radius-control); color: var(--muted); font-size: var(--font-small); text-align: left; }
.knowledge-entry > span:first-of-type { flex: 1; }
.knowledge-entry:hover { background: var(--hover); color: var(--accent); }
.nav-badge { min-width: 20px; padding: 1px 6px; border-radius: var(--radius-small); background: var(--warning-bg); color: var(--warning); font-size: var(--font-caption); text-align: center; }
.history-label { display: flex; align-items: center; justify-content: space-between; padding: 0 12px 10px; font-size: var(--font-caption); color: var(--muted); }
.history-count { font-variant-numeric: tabular-nums; color: var(--muted); font-size: var(--font-caption); }
.conversation-search { margin: 0 4px 16px; width: calc(100% - 8px); }
.conv-list { list-style: none; margin: 0; padding: 0; flex: 1; overflow-y: auto; min-height: 0; }
.conv-list li { display: flex; align-items: center; border-radius: var(--radius-control); margin-bottom: 4px; }
.conv-list .date-group, .conv-list .no-results { padding: 10px 12px; font-size: var(--font-caption); color: var(--muted); background: transparent; }
.conv-list .date-group { margin: 4px 0; font-size: var(--font-caption); }
.conv-list .no-results { padding: 24px 16px; text-align: center; line-height: 1.9; }
.conversation-item { flex-wrap: wrap; transition: background-color var(--duration-fast); border: 1px solid transparent; }
.conversation-item:hover { background: var(--hover); }
.conversation-item.active { background: var(--bg); border-color: var(--line); box-shadow: var(--shadow-sm); color: var(--accent); }
.conv-btn { display: flex; align-items: center; gap: 8px; flex: 1; min-width: 0; min-height: var(--control-height); padding: 9px 11px; text-align: left; font-size: var(--font-small); }
.conv-title { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.deleted-project-badge { flex-shrink: 0; color: var(--muted); font-size: var(--font-caption); }
.conversation-progress { flex-shrink: 0; width: 6px; height: 6px; margin-left: auto; border-radius: 50%; background: var(--accent); animation: pulse 1.3s ease-in-out infinite; }
.conv-more { margin-right: 4px; opacity: 0; }
.conversation-item:hover .conv-more, .conversation-item:focus-within .conv-more, .conversation-item.active .conv-more, .conversation-item.menu-open .conv-more { opacity: 1; }
.conversation-menu { flex-basis: 100%; margin: 0 4px 4px; box-shadow: var(--shadow-sm); }
.conv-list .conversation-pagination { justify-content: center; margin: 12px 8px 4px; padding-top: 12px; border-top: 1px solid var(--line); border-radius: 0; background: transparent; }
.load-more-button { color: var(--muted); }
.load-more-button:disabled { cursor: wait; }
.sidebar-footer { margin-top: auto; padding: 12px 4px 0; }
.divider { height: 1px; background: var(--line); margin-bottom: 16px; }
.user-area, .user-info { display: flex; align-items: center; gap: 10px; min-width: 0; }
.user-info { flex: 1; }
.user-avatar { width: 36px; height: 36px; background: var(--accent-soft); border: 1px solid var(--accent-border); color: var(--accent); border-radius: var(--radius-card); display: grid; place-items: center; flex-shrink: 0; font-size: var(--font-body); font-weight: 600; }
.user-details { display: flex; flex-direction: column; min-width: 0; }
.user-name { font-size: var(--font-small); font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.user-space { font-size: var(--font-caption); color: var(--muted); }
.gear-wrapper { position: relative; }
.gear-menu { position: absolute; bottom: calc(100% + 8px); right: 0; width: 200px; max-height: calc(100dvh - 128px - env(safe-area-inset-bottom, 0px)); overflow-y: auto; overscroll-behavior: contain; z-index: 30; }
.btn-search.active { background: var(--hover); color: var(--accent); }
.btn-gear { position: relative; }
.badge { background: var(--danger); color: var(--on-accent); border-radius: var(--radius-panel); font-size: var(--font-caption); padding: 1px 4px; position: absolute; top: -3px; right: -3px; }
.menu-badge { position: static; margin-left: 8px; }
.rename-form { gap: 16px; }
.rename-form input { width: 100%; min-width: 0; }
.rename-form button { align-self: flex-end; }
@media (min-width: 761px) { .sidebar.collapsed { display: none; } }
@media (max-width: 760px) {
  .sidebar { position: fixed; inset: 0 auto 0 0; width: min(300px, calc(100vw - 40px)); z-index: 50; visibility: hidden; transform: translateX(-100%); transition: transform .2s, visibility .2s; padding-bottom: max(16px, env(safe-area-inset-bottom)); box-shadow: var(--shadow-lg); }
  .sidebar.open { visibility: visible; transform: translateX(0); }
}
@media (hover: none) { .conv-more { opacity: 1; } }
</style>
