import { onUnmounted, ref } from 'vue'

/** One replaceable notice per owner; stale timers cannot dismiss a newer notice. */
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
