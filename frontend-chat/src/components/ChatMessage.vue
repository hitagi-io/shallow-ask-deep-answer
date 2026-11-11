<script setup lang="ts">
import { computed, ref } from 'vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { isSourceUrl, type Message } from '../api'
import { formatDuration } from '../utils/duration'
import BrandMark from './BrandMark.vue'
import { copyText } from '../utils/clipboard'
import { useNotice } from '../composables/useNotice'
import { useStreamingText } from '../composables/useStreamingText'
import SourcePassageDialog from './SourcePassageDialog.vue'
import { collapseSourceReferences } from '../utils/sourceReferences'

const props = defineProps<{
  message: Message
  projectDeleted: boolean
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
const sourceUrl = ref('')
async function copy(text: string) {
  try { await copyText(text); show('已复制', 'success') }
  catch { show('复制失败，请手动选择文本复制', 'error') }
}
function copyCode(event: MouseEvent) {
  const button = (event.target as HTMLElement).closest<HTMLButtonElement>('button[data-copy-code]')
  if (button && body.value?.contains(button)) copy(button.parentElement?.querySelector('code')?.textContent || '')
  const link = (event.target as HTMLElement).closest<HTMLAnchorElement>('a[href]')
  const href = link?.getAttribute('href') || ''
  if (link && body.value?.contains(link) && isSourceUrl(href)) {
    event.preventDefault()
    sourceUrl.value = href
  }
}
// 空内容时按终态给出占位文案：服务端只保存模型实际输出，不拼接展示标记
function bubbleText(msg: Message) {
  if (msg.content) return msg.content
  if (msg.status === 'error') return '这次没能完成回答，请重试。'
  if (msg.status === 'cancelled') return '已停止回答。'
  if (msg.status === 'completed') return '这次没有收到回答，请重新提问。'
  return ''
}

const renderedText = useStreamingText(() => bubbleText(props.message), () => props.message.status === 'streaming')
// 历史消息复用 HTML；流式全文最多每 100ms 解析一次，仍保留 HTML 清洗。
const html = computed(() => {
  // 最终校验后服务端会替换为编号链接；生成中隐藏完整及尚未收齐的内部引用标记。
  const text = props.message.kbOnly
    ? renderedText.value.replace(/\[\[cite:[^\]]*(?:\]\]|$)/g, '')
    : renderedText.value
  let clean = DOMPurify.sanitize(marked.parse(text, { breaks: true, gfm: true }) as string, { FORBID_TAGS: ['button'], ALLOW_DATA_ATTR: false })
  if (props.projectDeleted || (props.message.kbOnly && clean.includes('参考来源（点击查看原文）'))) {
    const doc = new DOMParser().parseFromString(clean, 'text/html')
    collapseSourceReferences(doc, props.projectDeleted)
    if (props.projectDeleted) {
      for (const link of doc.querySelectorAll('a[href]')) {
        if (!isSourceUrl(link.getAttribute('href') || '')) continue
        link.append('（已删除）')
        link.setAttribute('title', '来源已删除，点击查看说明')
        link.classList.add('source-deleted')
      }
      for (const paragraph of doc.querySelectorAll('p')) {
        if (paragraph.textContent === '参考来源（点击查看原文）') paragraph.textContent = '参考来源（已删除，点击查看说明）'
      }
    }
    clean = doc.body.innerHTML
  }
  return clean.replace(/<pre>/g, '<pre><button type="button" class="icon-button" data-copy-code aria-label="复制代码" title="复制代码"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1"/></svg></button>')
})
</script>

<template>
  <SourcePassageDialog v-if="sourceUrl" :url="sourceUrl" :project-deleted="projectDeleted" @close="sourceUrl = ''" />
  <div class="message" :class="message.role">
    <div v-if="message.role === 'assistant'" class="avatar">
      <BrandMark />
    </div>
    <div class="bubble-wrap">
      <div v-if="message.role === 'assistant'" class="message-author">浅问深答<span v-if="streaming" class="streaming-label">正在回答</span></div>
      <div v-if="message.role === 'assistant' && thinking" class="thinking" role="status">
        <span class="thinking-dot"></span>
        <span class="thinking-dot" style="animation-delay:0.15s"></span>
        <span class="thinking-dot" style="animation-delay:0.3s"></span>
        <span class="thinking-text">{{ thinkingPhase || '正在处理问题' }}… {{ formatDuration(thinkingSeconds) }}</span>
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
        <button v-if="!projectDeleted && (message.status === 'error' || message.status === 'cancelled')" class="icon-button" :disabled="busy" aria-label="重新提问" title="重新提问" @click="emit('retry')">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 7v5h-5M4 17v-5h5"/><path d="M6.1 6.1A8 8 0 0 1 19.6 9M4.4 15a8 8 0 0 0 13.5 2.9"/></svg>
        </button>
        <span role="status" aria-live="polite">{{ copyNotice }}</span>
      </div>
      <div v-if="message.role === 'assistant' && message.thinkingTime != null && message.content" class="thinking-time">用时 {{ formatDuration(message.thinkingTime) }}</div>
      <div v-if="message.role === 'assistant' && (message.status === 'cancelled' || message.status === 'error') && message.content" class="message-note" :class="message.status">
        {{ message.status === 'cancelled' ? '已停止回答' : projectDeleted ? '回答未完成' : '回答未完成，可重新提问' }}
      </div>
      <div v-if="message.role === 'user'" class="bubble">{{ message.content }}</div>
      <span v-if="streaming && message.role === 'assistant'" class="cursor">▌</span>
    </div>
  </div>
</template>

<style scoped>
.message-author { display: flex; align-items: center; gap: 10px; margin: 0 0 8px; color: var(--accent); font-size: 12px; line-height: 1.5; font-weight: 600; }
.streaming-label { color: var(--muted); font-size: 10px; font-weight: 400; }
.message-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 4px; font-size: 12px; color: var(--muted); margin-top: 14px; }
.message-actions [role="status"] { margin-left: 8px; overflow-wrap: anywhere; }
.feedback-button { transition: background-color var(--duration-fast), color var(--duration-fast), transform var(--duration-fast); }
.feedback-button.selected { color: var(--accent); background: var(--accent-soft); }
.feedback-button.selected svg { fill: currentColor; }
.feedback-button:active { transform: scale(.9); }
@media (prefers-reduced-motion: reduce) {
  .feedback-button { transition: none; }
  .feedback-button:active { transform: none; }
}
.markdown-body :deep(pre button) { margin: 0 0 10px auto; color: var(--muted); }
.message { display: flex; gap: 14px; max-width: 768px; margin: 0 auto 32px; }
.avatar { width: 32px; height: 32px; display: grid; place-items: center; flex-shrink: 0; border: 1px solid var(--accent-border); border-radius: 10px; background: var(--accent-soft); }
.avatar .brand-mark { width: 22px; height: 22px; color: var(--accent); }
.message.user { justify-content: flex-end; }
.bubble-wrap { min-width: 0; flex: 1; }
.message.user .bubble-wrap { flex: 0 1 auto; max-width: 85%; }
.bubble { font-size: 15px; line-height: 1.85; overflow-wrap: anywhere; }
.message.user .bubble { background: var(--accent-soft); border: 1px solid var(--accent-border); border-radius: 18px; padding: 12px 18px; color: var(--text); white-space: pre-wrap; }
.thinking { display: flex; gap: 5px; align-items: center; min-height: 32px; }
.thinking-dot { flex-shrink: 0; width: 4px; height: 4px; background: var(--accent); border-radius: 50%; animation: pulse 1.3s infinite; }
.thinking-text { color: var(--muted); font-size: 12px; margin-left: 6px; }
.thinking-time { display: inline-block; font-size: 11px; color: var(--muted); margin-top: 8px; }
.message-note { display: inline-block; padding: 2px 8px; border-radius: 6px; background: var(--surface); font-size: 11px; color: var(--muted); margin: 8px 0 0 8px; }
.message-note.error { color: var(--danger); }
.cursor { color: var(--accent); animation: pulse 1s infinite; }
.markdown-body :deep(p) { margin: 0 0 14px; }
.markdown-body :deep(p:last-child) { margin-bottom: 0; }
.markdown-body :deep(h1), .markdown-body :deep(h2), .markdown-body :deep(h3), .markdown-body :deep(h4) { line-height: 1.5; font-weight: 650; margin: 24px 0 12px; letter-spacing: -.3px; }
.markdown-body :deep(h1) { font-size: 23px; }
.markdown-body :deep(h2) { font-size: 20px; }
.markdown-body :deep(h3) { font-size: 17px; }
.markdown-body :deep(h4) { font-size: 15px; }
.markdown-body :deep(> :first-child) { margin-top: 0; }
.markdown-body :deep(ul), .markdown-body :deep(ol) { padding-left: 24px; margin: 12px 0; }
.markdown-body :deep(li) { margin: 5px 0; }
.markdown-body :deep(a[href]) { color: var(--link); font-weight: 500; text-decoration: underline; text-decoration-color: currentColor; text-decoration-thickness: 1px; text-underline-offset: 3px; border-radius: 3px; transition: color var(--duration-fast), background-color var(--duration-fast); }
.markdown-body :deep(a[href]:hover), .markdown-body :deep(a[href]:focus-visible) { color: var(--link-hover); background-color: var(--link-soft); text-decoration-thickness: 2px; }
.markdown-body :deep(a[href]:focus-visible) { outline: 2px solid var(--link); outline-offset: 3px; }
.markdown-body :deep(a.source-deleted) { color: var(--muted); }
.markdown-body :deep(.source-references-heading) { margin-bottom: 6px; color: var(--muted); font-size: 13px; }
.markdown-body :deep(.source-file) { margin: 2px 0; }
.markdown-body :deep(.source-file summary) { display: flex; align-items: baseline; gap: 9px; width: fit-content; max-width: 100%; padding: 5px 6px; margin-left: -6px; border-radius: 4px; color: var(--link); font-size: 13px; font-weight: 500; cursor: pointer; list-style: none; }
.markdown-body :deep(.source-file summary::-webkit-details-marker) { display: none; }
.markdown-body :deep(.source-file summary::before) { content: ''; flex: 0 0 6px; width: 6px; height: 6px; border-right: 1.5px solid currentColor; border-bottom: 1.5px solid currentColor; transform: translateY(-1px) rotate(-45deg); }
.markdown-body :deep(.source-file[open] > summary::before) { transform: translateY(-3px) rotate(45deg); }
.markdown-body :deep(.source-file summary span) { min-width: 0; text-decoration: underline; text-underline-offset: 3px; }
.markdown-body :deep(.source-file summary:hover) { color: var(--link-hover); background: var(--link-soft); }
.markdown-body :deep(.source-file summary:focus-visible) { outline: 2px solid var(--link); outline-offset: 2px; }
.markdown-body :deep(.source-file > ul) { margin: 2px 0 10px 3px; padding-left: 24px; font-size: 13px; }
.markdown-body :deep(.source-file > ul > li) { margin: 3px 0; }
.markdown-body :deep(blockquote) { border-left: 3px solid var(--accent-border); border-radius: 0 10px 10px 0; background: var(--surface); padding: 12px 16px; margin: 18px 0; color: var(--muted); }
.markdown-body :deep(code) { font-family: var(--font-mono); font-size: .86em; background: var(--surface); padding: 3px 6px; border-radius: 5px; }
.markdown-body :deep(pre) { max-width: 100%; background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius-card); padding: 14px 16px; overflow-x: auto; line-height: 1.65; }
.markdown-body :deep(pre code) { background: none; padding: 0; }
.markdown-body :deep(table) { display: block; max-width: 100%; overflow-x: auto; border-collapse: collapse; margin: 18px 0; font-size: 13px; line-height: 1.7; }
.markdown-body :deep(th), .markdown-body :deep(td) { border: 1px solid var(--line); padding: 10px 14px; text-align: left; }
.markdown-body :deep(th) { background: var(--surface); color: var(--text); font-weight: 600; white-space: nowrap; }
.markdown-body :deep(tbody tr:nth-child(even)) { background: var(--canvas); }
.markdown-body :deep(hr) { border: 0; border-top: 1px solid var(--line); margin: 24px 0; }
.markdown-body :deep(img) { max-width: 100%; height: auto; border-radius: var(--radius-control); }
@media (max-width: 760px) {
  .message { gap: 10px; margin-bottom: 28px; }
  .avatar { width: 28px; height: 28px; border-radius: 9px; }
  .avatar .brand-mark { width: 19px; height: 19px; }
  .message.user .bubble-wrap { max-width: 92%; }
  .message.user .bubble { padding: 10px 14px; }
  .bubble { font-size: 14px; }
  .markdown-body :deep(pre) { padding: 12px; }
  .markdown-body :deep(th), .markdown-body :deep(td) { padding: 8px 10px; }
}
</style>
