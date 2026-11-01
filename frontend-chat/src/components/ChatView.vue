<script setup lang="ts">
import { nextTick } from 'vue'
import BrandMark from './BrandMark.vue'
import ChatMessage from './ChatMessage.vue'
import type { useChat } from '../composables/useChat'

const props = defineProps<{ chat: ReturnType<typeof useChat> }>()
const { input, inputEl, chatArea, current, isProcessing, isThinking, thinkingSeconds, thinkingPhase,
  kbOnly, suggestions, useSuggestion, sendMessage, stopStreaming, onKeydown, persistNotice, clearNotice,
  loading, loadError, initConversations, conversationBusy, following, onScroll, jumpToLatest,
  loadMessages, hasEarlier, visibleMessages, messageOffset, showEarlier, retryMessage, canRetrySend, retryFailedSend } = props.chat
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
  <div class="chat-region">
  <div ref="chatArea" class="chat-area" tabindex="0" aria-label="对话消息" @scroll.passive="onScroll">
    <div v-if="loading" class="empty" role="status">正在加载会话…</div>
    <div v-else-if="loadError" class="empty" role="alert"><p>{{ loadError }}</p><button class="text-action" @click="initConversations">重新加载</button></div>
    <template v-if="current && !loading && !loadError">
      <div v-if="current.messagesLoading && !current.messagesLoaded" class="empty" role="status">正在加载消息…</div>
      <div v-if="current.messagesError" role="alert" class="history-button">
        {{ current.messagesError }}
        <button class="text-action" :disabled="current.messagesLoading" @click="current.messagesLoaded ? showEarlier() : loadMessages()">重试加载</button>
      </div>
      <div v-if="current.messagesLoaded && current.messages.length === 0" class="empty welcome-state">
        <BrandMark class="empty-seal" />
        <p class="empty-title">有什么想了解的？</p>
        <p class="empty-hint">从一个问题开始，让知识为你所用。</p>
        <div class="suggestions"><button v-for="(suggestion, i) in suggestions" :key="suggestion.label" @click="useSuggestion(suggestion.prompt)"><span class="suggestion-index">0{{ i + 1 }}</span><span>{{ suggestion.label }}</span><span aria-hidden="true">↗</span></button></div>
      </div>

      <button v-if="hasEarlier" class="history-button text-action" :disabled="current.messagesLoading || isProcessing" @click="showEarlier">{{ current.messagesLoading ? '正在加载…' : '查看更早的消息' }}</button>
      <ChatMessage v-for="(msg, i) in visibleMessages" :key="`${current.id}:${msg.id ?? `local-${messageOffset + i}`}`"
        :message="msg" :thinking="isThinking && i === visibleMessages.length - 1"
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
  <button v-if="canRetrySend" class="latest-button" :disabled="isProcessing" @click="retryFailedSend">重试未确认的发送</button>

  <transition name="banner">
    <div v-if="persistNotice" class="persist-notice" role="alert">
      {{ persistNotice }} <button aria-label="关闭提示" @click="clearNotice">×</button>
    </div>
  </transition>

  <div class="input-bar">
    <div class="input-inner">
      <div class="input-options">
        <div class="mode-switch" role="group" aria-label="问答模式">
          <button :class="{ active: kbOnly }" :aria-pressed="kbOnly" :disabled="isProcessing" @click="kbOnly = true">知识库问答</button>
          <button :class="{ active: !kbOnly }" :aria-pressed="!kbOnly" :disabled="isProcessing" @click="kbOnly = false">通用问答</button>
        </div>
        <span class="mode-description">{{ kbOnly ? '问候、身份询问、翻译改写直接回答；其他问题检索内部资料' : '模型知识 · 按需联网' }}</span>
      </div>
      <div class="input-row">
        <textarea
          ref="inputEl"
          v-model="input" :placeholder="isProcessing ? '可以先写下一个问题…' : kbOnly ? '向知识库提问…' : '询问、探索，或开始一个想法…'" aria-label="输入问题" rows="1"
          @keydown="onKeydown"
          @input="resizeInput"
          />
        <button v-if="input.length" type="button" class="btn-clear" @click="clearInput" aria-label="清空输入框" title="清空输入框">清空</button>
        <button v-if="isProcessing" class="btn-send" @click="stopStreaming" aria-label="停止生成" title="停止生成">
          <svg width="12" height="12" viewBox="0 0 24 24" aria-hidden="true"><rect x="5" y="5" width="14" height="14" rx="3" fill="currentColor"/></svg>
        </button>
        <button v-else class="btn-send" :disabled="!input.trim() || !current?.messagesLoaded || current?.messagesLoading || loading || conversationBusy" @click="sendMessage()" aria-label="发送消息" title="发送消息">↑</button>
      </div>
    </div>
    <p class="composer-hint">Enter 发送 <span>·</span> Shift + Enter 换行 <span>·</span> 重要信息请核对来源</p>
  </div>
</template>

<style scoped>
.history-button { display: block; margin: 0 auto 28px; }
.chat-region { position: relative; display: flex; flex-direction: column; flex: 1; min-height: 0; overflow: hidden; }
.chat-navigation { position: absolute; right: max(8px, calc((100% - 768px) / 2 - 48px)); bottom: 16px; }
.jump-latest { flex-shrink: 0; display: grid; place-items: center; width: 36px; height: 36px; border: 1px solid var(--line); border-radius: 50%; background: var(--bg); color: var(--muted); box-shadow: 0 2px 8px #0001; }
.jump-latest:hover { background: var(--surface); color: var(--text); }
.latest-button { align-self: center; padding: 8px 16px; border: 1px solid var(--line); border-radius: 24px; background: var(--bg); font-size: 12px; box-shadow: 0 2px 8px #0001; }
.chat-area { flex: 1; min-height: 0; overflow-y: auto; padding: 52px 32px 24px; }
.welcome-state { padding: 24px 0 clamp(48px, calc(10vh - 24px), 80px); }
.empty-seal { width: 42px; height: 42px; margin-bottom: 20px; }
.empty-title { font-size: clamp(25px, 3vw, 32px); letter-spacing: -1px; font-weight: 600; margin: 0 0 12px; }
.empty-hint { font-size: 14px; color: var(--muted); margin: 0; }
.suggestions { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; width: 100%; max-width: 680px; margin-top: 36px; }
.suggestions button { border: 1px solid var(--line); border-radius: 10px; text-align: left; padding: 15px; display: flex; flex-wrap: wrap; align-items: center; gap: 13px; font-size: 12px; line-height: 1.6; transition: background .15s, border-color .15s; }
.suggestions button:hover { background: var(--surface); border-color: #ccc; }
.suggestion-index { width: 100%; color: #969696; font-family: var(--font-mono); font-size: 11px; }
.suggestions button > span:nth-child(2) { flex: 1; }
.suggestions button > span:last-child { color: #949494; }
.input-bar { padding: 12px 32px 16px; }
.input-inner { display: flex; flex-direction: column; max-width: 768px; margin: 0 auto; border: 1px solid #dedede; border-radius: 17px; padding: 10px 14px; box-shadow: 0 4px 20px #00000005; background: white; }
.input-inner:focus-within { border-color: #aaa; box-shadow: 0 4px 20px #00000008; }
.input-row { order: 0; display: flex; gap: 12px; align-items: flex-end; }
.input-row textarea { width: 100%; min-width: 0; resize: none; border: 0; background: transparent; padding: 6px 0; font-size: 14px; line-height: 1.7; min-height: 48px; max-height: 120px; }
.input-row textarea:focus-visible { outline: none; }
.btn-clear { flex-shrink: 0; height: 34px; padding: 0 8px; border-radius: 7px; color: var(--muted); font-size: 12px; }
.btn-clear:hover { background: var(--surface); color: var(--text); }
.btn-send { width: 34px; height: 34px; border-radius: 50%; background: var(--accent); color: white; flex-shrink: 0; display: grid; place-items: center; font-size: 22px; }
.btn-send:hover:not(:disabled) { background: #444; }
.input-options { order: 1; display: flex; align-items: center; gap: 12px; margin-top: 8px; }
.mode-switch { display: flex; gap: 3px; background: #f4f4f4; padding: 3px; border-radius: 7px; }
.mode-switch button { font-size: 11px; color: var(--muted); padding: 5px 9px; border-radius: 5px; }
.mode-switch button.active { background: white; color: var(--text); box-shadow: 0 1px 3px #00000012; }
.mode-description { font-size: 11px; color: var(--muted); }
.composer-hint { font-size: 10px; color: #858585; text-align: center; margin: 11px 0 0; }
.composer-hint span { margin: 0 6px; color: #b5b5b5; }
.persist-notice { margin: 0 auto; padding: 8px 16px; color: var(--danger); background: #fff4f2; font-size: 12px; border-radius: 8px; max-width: 90%; cursor: pointer; }
@media (max-width: 760px) {
  .chat-area { padding: 40px 18px 24px; }
  .empty-title { font-size: 26px; }
  .suggestions { grid-template-columns: 1fr; max-width: 360px; gap: 8px; margin-top: 26px; }
  .suggestions button { padding: 11px 14px; }
  .suggestion-index { width: auto; }
  .input-bar { padding: 8px 12px max(12px, env(safe-area-inset-bottom)); }
  .input-inner { padding: 10px 12px; }
  .mode-description { display: none; }
}
@media (max-height: 650px) {
  .welcome-state { padding: 8px 0; }
  .empty-seal { display: none; }
  .suggestions { margin-top: 20px; }
}
</style>
