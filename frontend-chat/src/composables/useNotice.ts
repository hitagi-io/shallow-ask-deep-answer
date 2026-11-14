import { onUnmounted, ref } from 'vue'

/** 每个调用方只保留一条可替换的提示；错误默认保留，临时警报可指定收起时间。 */
export function useNotice(duration = 4000, options: { errorDuration?: number } = {}) {
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
    const timeout = kind === 'error' ? options.errorDuration ?? 0 : duration
    if (timeout > 0) timer = setTimeout(clear, timeout)
  }
  onUnmounted(() => { disposed = true; clear() })
  return { message, type, show, clear }
}
