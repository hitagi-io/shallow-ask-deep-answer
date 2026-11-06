<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref, useId } from 'vue'
import AppIcon from './AppIcon.vue'
defineProps<{ title: string; compact?: boolean }>()
const emit = defineEmits<{ close: [] }>()
const dialog = ref<HTMLDialogElement | null>(null)
const titleId = useId()
const previousFocus = document.activeElement as HTMLElement | null
let pointerStartedOutside = false
function isOutside(event: MouseEvent) {
  const element = dialog.value
  if (!element || event.target !== element) return false
  const bounds = element.getBoundingClientRect()
  return event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom
}
function onDialogClick(event: MouseEvent) {
  if (pointerStartedOutside && isOutside(event)) emit('close')
  pointerStartedOutside = false
}
onMounted(() => dialog.value?.showModal())
onBeforeUnmount(() => { dialog.value?.close(); if (previousFocus?.isConnected) previousFocus.focus() })
</script>

<template>
  <dialog ref="dialog" :class="{ compact }" :aria-labelledby="titleId" @cancel.prevent="emit('close')" @pointerdown="pointerStartedOutside = isOutside($event)" @click="onDialogClick">
    <header><h2 :id="titleId">{{ title }}</h2><button type="button" class="icon-button" aria-label="关闭对话框" title="关闭对话框" @click="emit('close')"><AppIcon name="close" /></button></header>
    <slot />
  </dialog>
</template>

<style scoped>
dialog { border: 1px solid var(--line); border-radius: var(--radius-panel); padding: 28px; width: min(720px, calc(100vw - 40px)); max-height: calc(100dvh - 64px); overflow-y: auto; overscroll-behavior: contain; color: var(--text); background: var(--bg); box-shadow: var(--shadow-lg); }
dialog::backdrop { background: var(--overlay); backdrop-filter: blur(3px); }
dialog.compact { width: min(440px, calc(100vw - 40px)); padding: 24px; }
header { display: flex; align-items: center; gap: 16px; margin-bottom: 22px; padding-bottom: 18px; border-bottom: 1px solid var(--line); }
h2 { flex: 1; margin: 0; font-size: 18px; line-height: 1.5; font-weight: 600; letter-spacing: -.3px; overflow-wrap: anywhere; }
header .icon-button { width: 34px; height: 34px; margin: -4px -4px -4px 0; background: var(--surface); border-radius: var(--radius-control); }
header .icon-button:hover { background: var(--hover); }
@media (max-width: 520px) {
  dialog, dialog.compact { width: calc(100vw - 28px); max-height: calc(100dvh - 32px); padding: 20px; border-radius: var(--radius-card); }
  header { margin-bottom: 18px; padding-bottom: 14px; }
  h2 { font-size: 17px; }
}
</style>
