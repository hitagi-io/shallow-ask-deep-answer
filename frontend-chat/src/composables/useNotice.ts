import { onUnmounted, ref } from 'vue'

/** 每个调用方只保留一条可替换的提示；旧计时器不能关闭新提示。 */
export function useNotice(duration = 4000) {
  const message = ref('')
  const type = ref<'success' | 'error' | ''>('')
  let timer: ReturnType<typeof setTimeout> | undefined
  let disposed = false
  function clear() {
    clearTimeout(timer)
    message.value = ''
    type.value = ''
  }
  function show(text: string, kind: 'success' | 'error') {
    if (disposed) return
    clear()
    message.value = text
    type.value = kind
    if (duration > 0 && kind !== 'error') timer = setTimeout(clear, duration)
  }
  onUnmounted(() => { disposed = true; clear() })
  return { message, type, show, clear }
}
