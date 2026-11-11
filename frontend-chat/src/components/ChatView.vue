<script setup lang="ts">
import { computed, nextTick } from 'vue'
import ChatMessage from './ChatMessage.vue'
import BrandMark from './BrandMark.vue'
import AppIcon from './AppIcon.vue'
import AppNotice from './AppNotice.vue'
import type { useChat } from '../composables/useChat'
import type { useProjects } from '../composables/useProjects'

const props = defineProps<{ chat: ReturnType<typeof useChat>; projects: ReturnType<typeof useProjects> }>()
const emit = defineEmits<{ manage: [] }>()
const { projects: projectList, loading: projectsLoading, error: projectsError, load: reloadProjects } = props.projects
const { input, inputEl, chatArea, current, isProcessing, isThinking, thinkingSeconds, thinkingPhase, reconnecting, stopping,
  kbOnly, switchMode, sendMessage, stopStreaming, onKeydown, persistNotice, clearNotice, projectId, selectedProject, projectReady, projectDeleted, selectProject, newConversation,
  loading, loadError, initConversations, conversationBusy, following, onScroll, jumpToLatest,
  loadMessages, hasEarlier, visibleMessages, messageOffset, showEarlier, retryMessage, canRetrySend, retryFailedSend } = props.chat
const suggestions = computed(() => kbOnly.value ? [
  { icon: 'file' as const, title: '提炼重点', detail: '快速读懂项目资料', prompt: '请总结这个项目资料中的核心内容，并列出关键要点。' },
  { icon: 'book' as const, title: '查找依据', detail: '让答案有据可查', prompt: '请帮我查找资料中关于以下问题的规定，并注明来源：' },
  { icon: 'folder' as const, title: '梳理关联', detail: '连接分散的知识', prompt: '请梳理这个项目中各份资料的关联，以及需要关注的差异。' },
] : [
  { icon: 'book' as const, title: '理解一个概念', detail: '把复杂问题讲清楚', prompt: '请用通俗的语言和具体例子，帮我理解这个概念：' },
  { icon: 'file' as const, title: '整理思路', detail: '从想法到清晰表达', prompt: '请帮我梳理以下想法，整理成结构清晰、重点明确的内容：' },
  { icon: 'folder' as const, title: '制定计划', detail: '把目标拆成下一步', prompt: '请帮我将这个目标拆解为可执行的步骤，并标出优先级：' },
])
async function useSuggestion(prompt: string) {
  input.value = prompt
  await nextTick()
  if (inputEl.value) {
    inputEl.value.style.height = 'auto'
    inputEl.value.style.height = `${Math.min(inputEl.value.scrollHeight, 120)}px`
    inputEl.value.focus()
  }
}
function resizeInput(event: Event) {
  const textarea = event.target as HTMLTextAreaElement
  textarea.style.height = 'auto'
  textarea.style.height = `${Math.min(textarea.scrollHeight, 120)}px`
}
async function clearInput() {
  input.value = ''
  await nextTick()
  if (inputEl.value) {
    inputEl.value.style.height = 'auto'
    inputEl.value.focus()
  }
}
</script>

<template>
  <header class="chat-header">
    <div class="chat-heading">
      <span class="chat-eyebrow">对话工作台</span>
      <h1 :title="current?.messages.length ? current.title : undefined">{{ current?.messages.length ? current.title : '开启新的思考' }}</h1>
    </div>
    <span class="mode-indicator" :class="{ 'knowledge-mode': kbOnly }"><span></span>{{ kbOnly ? '知识库问答' : '通用问答' }}</span>
  </header>
  <div class="chat-region">
  <div ref="chatArea" class="chat-area" :class="{ 'chat-area-empty': loading || !!loadError || !current?.messages.length }" tabindex="0" aria-label="对话消息" @scroll.passive="onScroll">
    <div v-if="loading" class="empty loading-state" role="status"><span class="loading-ring" aria-hidden="true"></span>正在加载会话…</div>
    <div v-else-if="loadError" class="empty" role="alert"><p>{{ loadError }}</p><button class="text-action" @click="initConversations">重新加载</button></div>
    <template v-if="current && !loading && !loadError">
      <div v-if="current.messagesLoading && !current.messagesLoaded" class="empty loading-state" role="status"><span class="loading-ring" aria-hidden="true"></span>正在加载消息…</div>
      <div v-if="current.messagesError" role="alert" class="history-button">
        {{ current.messagesError }}
        <button class="text-action" :disabled="current.messagesLoading" @click="current.messagesLoaded ? showEarlier() : loadMessages()">重试加载</button>
      </div>
      <div v-if="current.messagesLoaded && current.messages.length === 0" class="empty welcome-state">
        <div class="welcome-brand"><BrandMark /></div>
        <p class="welcome-kicker">浅问深答 · 让知识为你所用</p>
        <h2>{{ kbOnly ? '连接你的知识，找到可靠答案' : '每个好答案，都从一个问题开始' }}</h2>
        <p class="welcome-description">{{ kbOnly ? '选择一个项目，从资料中提炼重点、查找依据，发现新的联系。' : '理解复杂概念、整理零散想法，或一起探索下一步。' }}</p>
        <div class="suggestions" aria-label="提问建议">
          <button v-for="suggestion in suggestions" :key="suggestion.title" class="suggestion-card" :disabled="conversationBusy" @click="useSuggestion(suggestion.prompt)">
            <AppIcon :name="suggestion.icon" :size="19" />
            <strong>{{ suggestion.title }}</strong>
            <span>{{ suggestion.detail }}</span>
            <AppIcon name="arrow-up-right" :size="14" class="suggestion-arrow" />
          </button>
        </div>
      </div>

      <button v-if="hasEarlier" class="history-button text-action" :disabled="current.messagesLoading" @click="showEarlier">{{ current.messagesLoading ? '正在加载…' : '查看更早的消息' }}</button>
      <ChatMessage v-for="(msg, i) in visibleMessages" :key="`${current.id}:${kbOnly ? 'kb' : 'general'}:${msg.id ?? `local-${messageOffset + i}`}`"
        :message="msg" :project-deleted="projectDeleted" :thinking="isThinking && i === visibleMessages.length - 1"
        :streaming="isProcessing && !isThinking && i === visibleMessages.length - 1" :busy="isProcessing"
        :thinking-seconds="i === visibleMessages.length - 1 ? thinkingSeconds : 0" :thinking-phase="i === visibleMessages.length - 1 ? thinkingPhase : ''"
        @retry="retryMessage(messageOffset + i)" />
    </template>
  </div>
  <div v-if="current?.messages.length" class="chat-navigation">
  <button v-show="!following" type="button" class="jump-latest" aria-label="回到最新" title="回到最新" @click="jumpToLatest">
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 5v14m-6-6 6 6 6-6" /></svg>
  </button>
  </div>
  </div>
  <p v-if="reconnecting" class="composer-hint" role="status">连接中断，正在重新连接…</p>
  <button v-if="canRetrySend" class="latest-button" :disabled="isProcessing" @click="retryFailedSend">未能确认是否发送成功，点击重试</button>

  <transition name="banner">
    <AppNotice v-if="persistNotice" class="persist-notice" tone="error" dismissible @dismiss="clearNotice">{{ persistNotice }}</AppNotice>
  </transition>

  <div class="input-bar">
    <div v-if="projectDeleted" class="deleted-project-notice" role="status">
      <p><strong>{{ current?.project_name }}（已删除）</strong><br>历史对话仅供查看。文档和索引已删除，无法查看原文或继续提问。</p>
      <button class="btn btn-secondary" @click="newConversation(true)" :disabled="conversationBusy || loading">新建对话</button>
    </div>
    <div v-else class="input-inner">
      <div class="input-options">
        <button class="knowledge-toggle" :class="{ active: kbOnly }" :aria-pressed="kbOnly" :disabled="conversationBusy || loading || !current || !!loadError" @click="switchMode(!kbOnly)" title="选中使用知识库，取消使用通用问答">
          <AppIcon name="book" :size="15" />知识库
        </button>
        <label class="project-selector" :class="{ disabled: !kbOnly }">
          <AppIcon name="folder" :size="15" />
          <select :value="projectId || ''" aria-label="当前知识库项目" :disabled="!kbOnly || projectsLoading || conversationBusy || loading || !current || !!loadError" @change="selectProject(($event.target as HTMLSelectElement).value || null)">
            <option value="">{{ !kbOnly ? '通用模式 · 无需项目' : projectsLoading ? '正在加载项目…' : '选择项目' }}</option>
            <option v-if="projectId && !selectedProject" :value="projectId" disabled>项目已不可用</option>
            <option v-for="project in projectList" :key="project.id" :value="project.id">{{ project.name }}</option>
          </select>
        </label>
        <button class="manage-link" @click="emit('manage')" title="管理我的知识库">管理项目<AppIcon name="arrow-up-right" :size="13" /></button>
      </div>
      <div class="input-row">
        <textarea
          ref="inputEl"
          v-model="input" :placeholder="isProcessing ? '可以先写下一个问题…' : kbOnly ? (projectReady ? '想了解这个项目中的哪些内容？' : '请先选择项目，再开始知识库问答…') : '输入问题，或描述您想完成的事…'" aria-label="输入问题" rows="1"
          @keydown="onKeydown"
          @input="resizeInput"
          />
        <button v-if="input.length" type="button" class="btn-clear" @click="clearInput" aria-label="清空输入框" title="清空输入框">清空</button>
        <button v-if="isProcessing" :disabled="stopping" class="btn-send" @click="stopStreaming" aria-label="停止回答" title="停止回答">
          <svg width="12" height="12" viewBox="0 0 24 24" aria-hidden="true"><rect x="5" y="5" width="14" height="14" rx="3" fill="currentColor"/></svg>
        </button>
        <button v-else class="btn-send" :disabled="!input.trim() || !projectReady || !current?.messagesLoaded || current?.messagesLoading || loading || conversationBusy" @click="sendMessage()" aria-label="发送消息" :title="projectReady ? '发送消息' : '请先选择项目'"><AppIcon name="arrow-up" :size="20" /></button>
      </div>
    </div>
    <p v-if="kbOnly && projectsError" class="composer-hint project-error" role="alert">{{ projectsError }} <button class="text-action" @click="reloadProjects">重试</button></p>
    <p v-if="!projectDeleted" class="composer-hint composer-footer"><span class="keyboard-hint">Enter 发送<span class="hint-divider">·</span>Shift + Enter 换行</span><span>重要信息请核对来源</span></p>
  </div>
</template>

<style scoped>
.deleted-project-notice { max-width: 768px; margin: auto; padding: 16px; border: 1px solid var(--line); border-radius: var(--radius-control); color: var(--muted); font-size: 13px; line-height: 1.8; }
.deleted-project-notice p { margin: 0 0 12px; overflow-wrap: anywhere; }
.chat-header { display: flex; align-items: center; justify-content: space-between; gap: 24px; min-height: 76px; padding: 14px 32px 14px 68px; border-bottom: 1px solid var(--line); background: var(--bg); }
.chat-heading { min-width: 0; }
.chat-eyebrow { display: block; margin-bottom: 3px; color: var(--muted); font-size: 11px; letter-spacing: .06em; }
.chat-heading h1 { overflow: hidden; margin: 0; color: var(--text); font-size: 15px; font-weight: 600; line-height: 1.5; text-overflow: ellipsis; white-space: nowrap; }
.mode-indicator { display: inline-flex; align-items: center; gap: 7px; flex-shrink: 0; padding: 6px 10px; border: 1px solid var(--line); border-radius: 99px; color: var(--muted); font-size: 11px; }
.mode-indicator > span { width: 5px; height: 5px; border-radius: 50%; background: var(--subtle); }
.mode-indicator.knowledge-mode { color: var(--accent); border-color: var(--accent-border); background: var(--accent-soft); }
.mode-indicator.knowledge-mode > span { background: var(--accent); }
.history-button { display: block; margin: 0 auto 28px; max-width: 100%; text-align: center; overflow-wrap: anywhere; }
.chat-region { position: relative; display: flex; flex-direction: column; flex: 1; min-height: 0; overflow: hidden; container: chat-content / size; }
.chat-navigation { position: absolute; right: max(8px, calc((100% - 768px) / 2 - 48px)); bottom: 16px; }
.jump-latest { flex-shrink: 0; display: grid; place-items: center; width: 38px; height: 38px; border: 1px solid var(--line-strong); border-radius: 50%; background: var(--bg); color: var(--accent); box-shadow: var(--shadow-sm); }
.jump-latest:hover { background: var(--surface); color: var(--text); }
.latest-button { align-self: center; max-width: calc(100% - 24px); padding: 9px 16px; border: 1px solid var(--danger-border); border-radius: var(--radius-control); background: var(--danger-bg); color: var(--danger); font-size: 12px; }
.chat-area { flex: 1; min-height: 0; overflow-y: auto; padding: 36px 32px 24px; scroll-padding-bottom: 24px; }
/* Auto margins center short empty states and leave tall content scrollable from the top. */
.chat-area.chat-area-empty { display: flex; flex-direction: column; padding-block: 20px; }
.chat-area-empty > .empty { flex-shrink: 0; min-height: auto; width: 100%; margin-block: auto; padding-block: 0; }
.loading-state { gap: 14px; color: var(--muted); font-size: 13px; }
.loading-ring { width: 24px; height: 24px; border: 2px solid var(--line); border-top-color: var(--accent); border-radius: 50%; animation: spin .8s linear infinite; }
.welcome-state { max-width: 768px; margin-inline: auto; }
.welcome-brand { display: grid; place-items: center; width: 62px; height: 62px; margin-bottom: 22px; border: 1px solid var(--accent-border); border-radius: 20px; background: var(--accent-soft); }
.welcome-brand .brand-mark { width: 34px; height: 34px; color: var(--accent); }
.welcome-kicker { margin: 0 0 12px; color: var(--accent); font-size: 12px; font-weight: 550; letter-spacing: .1em; }
.welcome-state h2 { margin: 0; font-size: clamp(22px, 2.4vw, 30px); font-weight: 600; line-height: 1.5; letter-spacing: -.04em; text-wrap: balance; }
.welcome-description { max-width: 480px; margin: 14px 0 0; color: var(--muted); font-size: 13px; line-height: 1.85; text-wrap: balance; }
.suggestions { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; width: 100%; max-width: 636px; margin-top: 36px; }
.suggestion-card { position: relative; display: flex; flex-direction: column; align-items: flex-start; gap: 8px; min-width: 0; padding: 18px 16px; border: 1px solid var(--line); border-radius: var(--radius-card); background: var(--bg); text-align: left; transition: border-color var(--duration-fast), background-color var(--duration-fast), box-shadow var(--duration-fast); }
.suggestion-card > .app-icon { margin-bottom: 6px; color: var(--accent); }
.suggestion-card strong { font-size: 13px; font-weight: 600; }
.suggestion-card > span { color: var(--muted); font-size: 11px; line-height: 1.5; }
.suggestion-card .suggestion-arrow { position: absolute; top: 18px; right: 16px; color: var(--subtle); }
.suggestion-card:hover:not(:disabled) { border-color: var(--accent-border); background: var(--accent-soft); box-shadow: var(--shadow-sm); }
.input-bar { flex-shrink: 0; padding: 12px 32px 18px; }
.input-inner { display: flex; flex-direction: column; max-width: 768px; margin: 0 auto; border: 1px solid var(--line-strong); border-radius: var(--radius-panel); padding: 14px 16px 12px; box-shadow: var(--shadow-md); background: var(--bg); transition: border-color var(--duration-fast), box-shadow var(--duration-fast); }
.input-inner:focus-within { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft), var(--shadow-md); }
.input-row { order: 0; display: flex; gap: 8px; align-items: flex-end; }
.input-row textarea { width: 100%; min-width: 0; resize: none; border: 0; border-radius: 0; box-shadow: none; background: transparent; padding: 6px 0; font-size: 14px; line-height: 1.75; min-height: 52px; max-height: 120px; }
.input-row textarea:focus-visible { outline: none; }
.btn-clear { flex-shrink: 0; height: 38px; padding: 0 8px; border-radius: var(--radius-control); color: var(--muted); font-size: 12px; }
.btn-clear:hover { background: var(--surface); color: var(--text); }
.btn-send { width: 38px; height: 38px; border-radius: 12px; background: var(--accent); color: var(--bg); flex-shrink: 0; display: grid; place-items: center; transition: background-color var(--duration-fast); }
.btn-send:hover:not(:disabled) { background: var(--accent-hover); }
.input-options { order: 1; display: flex; align-items: center; gap: 12px; margin-top: 12px; padding-top: 10px; border-top: 1px solid var(--line); }
.knowledge-toggle { display: inline-flex; gap: 6px; align-items: center; min-height: 32px; padding: 5px 9px; border: 1px solid var(--line); border-radius: var(--radius-control); color: var(--muted); font-size: 12px; white-space: nowrap; }
.knowledge-toggle:hover:not(:disabled) { background: var(--surface); }
.knowledge-toggle.active { background: var(--accent); border-color: var(--accent); color: var(--on-accent); font-weight: 600; box-shadow: var(--shadow-sm); }
.knowledge-toggle.active:hover:not(:disabled) { background: var(--accent-hover); border-color: var(--accent-hover); }
.project-selector { display: flex; align-items: center; gap: 7px; flex: 0 1 180px; color: var(--muted); min-width: 0; }
.project-selector select { width: 100%; min-width: 0; border: 0; border-radius: 6px; background: transparent; font-size: 12px; color: var(--text); padding: 6px 18px 6px 0; cursor: pointer; text-overflow: ellipsis; }
.project-selector select:focus-visible { background-color: var(--surface); }
.project-selector.disabled { opacity: .65; }
.project-selector select:disabled { cursor: not-allowed; }
.manage-link { display: inline-flex; align-items: center; gap: 4px; min-height: 32px; margin-left: auto; padding: 4px; border-radius: 6px; font-size: 11px; color: var(--muted); white-space: nowrap; }
.manage-link:hover { color: var(--accent); background: var(--surface); }
.composer-hint { font-size: 11px; color: var(--muted); text-align: center; margin: 12px 0 0; line-height: 1.5; }
.composer-hint.project-error { color: var(--danger); }
.composer-footer { display: flex; justify-content: space-between; gap: 12px; max-width: 748px; margin: 12px auto 0; }
.hint-divider { padding: 0 8px; color: var(--subtle); }
.persist-notice { flex-shrink: 0; margin: 0 auto; width: min(768px, calc(100% - 64px)); }
@media (max-width: 760px) {
  .chat-header { min-height: 72px; padding: 12px 16px 12px 64px; gap: 12px; }
  .chat-heading h1 { font-size: 14px; }
  .mode-indicator { padding: 5px 8px; font-size: 10px; }
  .chat-area { padding: 24px 18px 16px; }
  .welcome-brand { width: 54px; height: 54px; margin-bottom: 18px; border-radius: 18px; }
  .welcome-state h2 { max-width: 320px; font-size: 24px; }
  .welcome-description { max-width: 320px; font-size: 12px; }
  .suggestions { gap: 8px; margin-top: 28px; }
  .suggestion-card { padding: 14px 12px; border-radius: 12px; }
  .suggestion-card strong { font-size: 12px; }
  .suggestion-card > span { font-size: 10px; }
  .suggestion-card .suggestion-arrow { display: none; }
  .input-bar { padding: 8px 12px max(12px, env(safe-area-inset-bottom)); }
  .persist-notice { width: calc(100% - 24px); }
  .input-inner { padding: 10px 12px; }
  .input-options { gap: 8px; flex-wrap: wrap; margin-top: 8px; }
  .project-selector { flex-basis: 160px; max-width: calc(100% - 96px); }
  .keyboard-hint { display: none; }
  .composer-footer { justify-content: center; font-size: 10px; margin-top: 10px; }
  .chat-navigation { right: 14px; bottom: 8px; }
}
@container chat-content (max-height: 480px) {
  .chat-area.chat-area-empty { padding-block: 12px; }
  .welcome-brand { width: 42px; height: 42px; margin-bottom: 12px; border-radius: 14px; }
  .welcome-brand .brand-mark { width: 28px; height: 28px; }
  .welcome-kicker { margin-bottom: 8px; }
  .welcome-state h2 { font-size: 22px; }
  .welcome-description { margin-top: 10px; }
  .suggestions { margin-top: 20px; }
  .suggestion-card { padding: 12px; }
}
@container chat-content (max-height: 360px) {
  .welcome-brand { display: none; }
  .suggestions { margin-top: 16px; }
}
@media (max-width: 380px) {
  .suggestion-card { gap: 6px; padding: 12px 8px; }
  .suggestion-card > .app-icon { margin-bottom: 2px; }
  .suggestion-card > span { display: none; }
  .manage-link { font-size: 10px; }
}
</style>
