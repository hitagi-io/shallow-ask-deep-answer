import { onUnmounted, ref } from 'vue'

export type ConfirmAction = (message: string) => Promise<boolean>

export function useConfirmation() {
  const message = ref('')
  let resolve: ((accepted: boolean) => void) | undefined
  function settle(accepted: boolean) {
    message.value = ''
    resolve?.(accepted)
    resolve = undefined
  }
  const ask: ConfirmAction = text => {
    settle(false)
    message.value = text
    return new Promise<boolean>(done => { resolve = done })
  }
  onUnmounted(() => settle(false))
  return { message, ask, settle }
}
