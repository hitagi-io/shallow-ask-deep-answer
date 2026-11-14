<script setup lang="ts">
import AppIcon from './AppIcon.vue'

defineProps<{ tone: 'success' | 'error' | 'warning'; dismissible?: boolean }>()
const emit = defineEmits<{ dismiss: [] }>()
</script>

<template>
  <div class="notice" :class="tone" :role="tone === 'error' ? 'alert' : 'status'">
    <AppIcon :name="tone === 'success' ? 'success' : 'error'" :size="20" />
    <p class="notice-message"><slot /></p>
    <button v-if="dismissible" type="button" class="icon-button notice-close" aria-label="关闭提示" title="关闭提示" @click="emit('dismiss')">
      <AppIcon name="close" :size="16" />
    </button>
  </div>
</template>

<style scoped>
.notice { display: flex; align-items: flex-start; gap: 8px; padding: 12px 16px; border: 1px solid var(--success-border); border-radius: var(--radius-control); background: var(--success-bg); color: var(--success); font-size: var(--font-small); line-height: 20px; }
.notice.error { border-color: var(--danger-border); background: var(--danger-bg); color: var(--danger); }
.notice.warning { border-color: var(--warning-border); background: var(--warning-bg); color: var(--warning); }
.notice-message { flex: 1; min-width: 0; max-height: 18rem; overflow-y: auto; margin: 0; white-space: pre-line; overflow-wrap: anywhere; }
.notice-close { margin: calc((20px - var(--control-small)) / 2) -8px calc((20px - var(--control-small)) / 2) 0; color: inherit; }
.notice-close:hover:not(:disabled) { background: rgb(0 0 0 / 5%); color: inherit; }
.notice-close:focus-visible { outline-color: currentColor; }
</style>
