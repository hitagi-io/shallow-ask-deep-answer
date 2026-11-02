<script setup lang="ts">
import { computed, ref } from 'vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import type { Message } from '../api'
import { formatDuration } from '../utils/duration'
import BrandMark from './BrandMark.vue'
import { copyText } from '../utils/clipboard'
import { useNotice } from '../composables/useNotice'
import { useStreamingText } from '../composables/useStreamingText'

const props = defineProps<{
  message: Message
  thinking: boolean
  streaming: boolean
  thinkingSeconds: number
  thinkingPhase: string
  busy: boolean
}>()
const emit = defineEmits<{ retry: [] }>()
const feedback = ref<'like' | 'dislike' | null>(null)
function toggleFeedback(value: 'like' | 'dislike') {
  feedback.value = feedback.value === value ? null : value
}
const { message: copyNotice, show } = useNotice(2500)
const body = ref<HTMLElement | null>(null)
async function copy(text: string) {
  try { await copyText(text); show('已复制', 'success') }
  catch { show('复制失败，请手动选择文本复制', 'error') }
}
function copyCode(event: MouseEvent) {
  const button = (event.target as HTMLElement).closest<HTMLButtonElement>('button[data-copy-code]')
  if (button && body.value?.contains(button)) copy(button.parentElement?.querySelector('code')?.textContent || '')
}
// 空内容时按终态给出占位文案：服务端只保存模型实际输出，不拼接展示标记
function bubbleText(msg: Message) {
  if (msg.content) return msg.content
  if (msg.status === 'error') return '（生成失败）'
  if (msg.status === 'cancelled') return '（已取消）'
  if (msg.status === 'completed') return '（未收到回复）'
  return ''
}

const renderedText = useStreamingText(() => bubbleText(props.message), () => props.message.status === 'streaming')
// 历史消息复用 HTML；流式全文最多每 100ms 解析一次，仍保留 HTML 清洗。
const html = computed(() => {
  const clean = DOMPurify.sanitize(marked.parse(renderedText.value, { breaks: true, gfm: true }) as string, { FORBID_TAGS: ['button'], ALLOW_DATA_ATTR: false })
  return clean.replace(/<pre>/g, '<pre><button type="button" class="icon-button" data-copy-code aria-label="复制代码" title="复制代码"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1"/></svg></button>')
})
</script>

<template>
  <div class="message" :class="message.role">
    <div v-if="message.role === 'assistant'" class="avatar">
      <BrandMark />
    </div>
    <div class="bubble-wrap">
      <div v-if="message.role === 'assistant' && thinking" class="thinking">
        <span class="thinking-dot"></span>
        <span class="thinking-dot" style="animation-delay:0.15s"></span>
        <span class="thinking-dot" style="animation-delay:0.3s"></span>
        <span class="thinking-text">{{ thinkingPhase || '处理中' }}...... {{ formatDuration(thinkingSeconds) }}</span>
      </div>
      <div v-if="message.role === 'assistant' && !(thinking && !message.content)" ref="body" class="bubble markdown-body" @click="copyCode" v-html="html"></div>
      <div v-if="message.role === 'assistant' && !thinking && !streaming" class="message-actions">
        <button v-if="message.content" class="icon-button" aria-label="复制回答" title="复制回答" @click="copy(message.content)">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1"/></svg>
        </button>
        <button v-if="message.content" type="button" class="icon-button feedback-button" :class="{ selected: feedback === 'like' }" :aria-pressed="feedback === 'like'" aria-label="点赞" :title="feedback === 'like' ? '取消点赞' : '点赞'" @click="toggleFeedback('like')">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 10v11H3V10h4Zm0 0 5-8a3 3 0 0 1 3 3l-1 5h5a2 2 0 0 1 2 2.4l-1.4 7A2 2 0 0 1 17.6 21H7"/></svg>
        </button>
        <button v-if="message.content" type="button" class="icon-button feedback-button" :class="{ selected: feedback === 'dislike' }" :aria-pressed="feedback === 'dislike'" aria-label="点踩" :title="feedback === 'dislike' ? '取消点踩' : '点踩'" @click="toggleFeedback('dislike')">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path transform="rotate(180 12 12)" d="M7 10v11H3V10h4Zm0 0 5-8a3 3 0 0 1 3 3l-1 5h5a2 2 0 0 1 2 2.4l-1.4 7A2 2 0 0 1 17.6 21H7"/></svg>
        </button>
        <button v-if="message.status === 'error' || message.status === 'cancelled'" class="icon-button" :disabled="busy" aria-label="重新提问" title="重新提问" @click="emit('retry')">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 7v5h-5M4 17v-5h5"/><path d="M6.1 6.1A8 8 0 0 1 19.6 9M4.4 15a8 8 0 0 0 13.5 2.9"/></svg>
        </button>
        <span role="status" aria-live="polite">{{ copyNotice }}</span>
      </div>
      <div v-if="message.role === 'assistant' && message.thinkingTime != null && message.content" class="thinking-time">· 处理耗时 {{ formatDuration(message.thinkingTime) }}</div>
      <div v-if="message.role === 'assistant' && (message.status === 'cancelled' || message.status === 'error') && message.content" class="message-note" :class="message.status">
        {{ message.status === 'cancelled' ? '· 已取消' : '· 回答未完成' }}
      </div>
      <div v-if="message.role === 'user'" class="bubble">{{ message.content }}</div>
      <span v-if="streaming && message.role === 'assistant'" class="cursor">▌</span>
    </div>
  </div>
</template>

<style scoped>
.message-actions { display: flex; align-items: center; gap: 4px; font-size: 12px; color: var(--muted); margin-top: 12px; }
.message-actions [role="status"] { margin-left: 8px; }
.feedback-button { transition: background-color .15s, color .15s, transform .15s; }
.feedback-button.selected { color: var(--text); }
.feedback-button.selected svg { fill: currentColor; }
.feedback-button:active { transform: scale(.9); }
@media (prefers-reduced-motion: reduce) {
  .feedback-button { transition: none; }
  .feedback-button:active { transform: none; }
}
.markdown-body :deep(pre button) { margin: 0 0 10px auto; }
.message { display: flex; gap: 12px; max-width: 768px; margin: 0 auto 36px; }
.avatar { width: 28px; height: 28px; display: grid; place-items: center; flex-shrink: 0; margin-top: 4px; }
.avatar .brand-mark { width: 22px; height: 22px; }
.message.user { justify-content: flex-end; }
.bubble-wrap { min-width: 0; flex: 1; }
.message.user .bubble-wrap { flex: 0 1 auto; max-width: 85%; }
.bubble { font-size: 15px; line-height: 1.85; overflow-wrap: anywhere; }
.message.user .bubble { background: #f3f3f3; border-radius: 16px; padding: 10px 18px; white-space: pre-wrap; }
.thinking { display: flex; gap: 5px; align-items: center; min-height: 32px; }
.thinking-dot { width: 4px; height: 4px; background: #777; border-radius: 50%; animation: pulse 1.3s infinite; }
.thinking-text { color: var(--muted); font-size: 12px; margin-left: 6px; }
.thinking-time { font-size: 11px; color: #898989; margin-top: 10px; }
.message-note { font-size: 11px; color: #898989; margin-top: 10px; }
.message-note.error { color: var(--danger); }
.cursor { animation: pulse 1s infinite; }
.markdown-body :deep(p) { margin: 0 0 14px; }
.markdown-body :deep(p:last-child) { margin-bottom: 0; }
.markdown-body :deep(h1), .markdown-body :deep(h2), .markdown-body :deep(h3), .markdown-body :deep(h4) { line-height: 1.5; font-weight: 650; margin: 24px 0 12px; letter-spacing: -.3px; }
.markdown-body :deep(h1) { font-size: 23px; }
.markdown-body :deep(h2) { font-size: 20px; }
.markdown-body :deep(h3) { font-size: 17px; }
.markdown-body :deep(h4) { font-size: 15px; }
.markdown-body :deep(ul), .markdown-body :deep(ol) { padding-left: 24px; margin: 12px 0; }
.markdown-body :deep(li) { margin: 5px 0; }
.markdown-body :deep(a) { color: #365e93; text-decoration: underline; text-underline-offset: 3px; }
.markdown-body :deep(blockquote) { border-left: 3px solid #ddd; padding-left: 16px; margin: 18px 0; color: #666; }
.markdown-body :deep(code) { font-family: var(--font-mono); font-size: .86em; background: #f1f1f1; padding: 3px 5px; border-radius: 4px; }
.markdown-body :deep(pre) { background: #f7f7f8; border: 1px solid var(--line); border-radius: 10px; padding: 16px; overflow-x: auto; line-height: 1.65; }
.markdown-body :deep(pre code) { background: none; padding: 0; }
.markdown-body :deep(table) { display: block; overflow-x: auto; border-collapse: collapse; margin: 18px 0; font-size: 13px; }
.markdown-body :deep(th), .markdown-body :deep(td) { border: 1px solid var(--line); padding: 9px 12px; text-align: left; }
.markdown-body :deep(th) { background: #f7f7f7; font-weight: 600; }
.markdown-body :deep(hr) { border: 0; border-top: 1px solid var(--line); margin: 24px 0; }
.markdown-body :deep(img) { max-width: 100%; border-radius: 8px; }
@media (max-width: 760px) {
  .message { gap: 8px; margin-bottom: 28px; }
  .message.user .bubble-wrap { max-width: 92%; }
  .bubble { font-size: 14px; }
}
</style>
