<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref } from 'vue'
defineProps<{ title: string; compact?: boolean }>()
const emit = defineEmits<{ close: [] }>()
const dialog = ref<HTMLDialogElement | null>(null)
const previousFocus = document.activeElement as HTMLElement | null
onMounted(() => dialog.value?.showModal())
onBeforeUnmount(() => { dialog.value?.close(); if (previousFocus?.isConnected) previousFocus.focus() })
</script>

<template>
  <dialog ref="dialog" :class="{ compact }" aria-labelledby="dialog-title" @cancel.prevent="emit('close')" @click="($event.target === dialog) && emit('close')">
    <header><h2 id="dialog-title">{{ title }}</h2><button aria-label="关闭对话框" @click="emit('close')">×</button></header>
    <slot />
  </dialog>
</template>

<style scoped>
dialog { border: 1px solid var(--line); border-radius: 16px; padding: 24px; width: min(720px, calc(100vw - 32px)); max-height: 85dvh; color: var(--text); background: var(--bg); box-shadow: 0 24px 80px #0003; }
dialog::backdrop { background: #0006; }
dialog.compact { width: min(400px, calc(100vw - 32px)); padding: 20px; border-radius: 12px; }
dialog.compact header { margin-bottom: 14px; }
dialog.compact h2 { font-size: 16px; }
header { display: flex; align-items: flex-start; gap: 16px; margin-bottom: 20px; }
h2 { flex: 1; margin: 0; font-size: 18px; overflow-wrap: anywhere; }
header button { font-size: 24px; line-height: 1; width: 30px; height: 30px; }
</style>
