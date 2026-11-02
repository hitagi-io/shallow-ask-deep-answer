import { onScopeDispose, ref, watch } from 'vue'

/** 限制流式 Markdown 的解析频率；结束、失败或取消时立即显示最终全文。 */
export function useStreamingText(source: () => string, streaming: () => boolean, interval = 100) {
  const text = ref('')
  let timer: ReturnType<typeof setTimeout> | undefined
  let renderedAt = -Infinity
  function flush() {
    clearTimeout(timer)
    timer = undefined
    text.value = source()
    renderedAt = performance.now()
  }
  watch([source, streaming], ([value, active]) => {
    if (value === text.value) {
      clearTimeout(timer)
      timer = undefined
      return
    }
    const remaining = interval - (performance.now() - renderedAt)
    if (!active || !text.value || remaining <= 0) flush()
    else if (timer === undefined) timer = setTimeout(flush, remaining)
  }, { immediate: true })
  onScopeDispose(() => clearTimeout(timer))
  return text
}
