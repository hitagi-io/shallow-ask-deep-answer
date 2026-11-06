<script setup lang="ts">
defineProps<{ title: string; description: string; busy?: boolean }>()
const emit = defineEmits<{ back: [] }>()
</script>

<template>
  <div class="settings-page">
    <header class="settings-header">
      <button class="icon-button" :disabled="busy" aria-label="返回首页" title="返回首页" @click="emit('back')">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M19 12H5m7 7-7-7 7-7" /></svg>
      </button>
      <div><p class="settings-eyebrow">浅问深答 / {{ title }}</p><h1>{{ title }}</h1><p class="settings-description">{{ description }}</p></div>
    </header>
    <div class="settings-content"><slot /></div>
  </div>
</template>

<style scoped>
.settings-page { flex: 1; min-height: 0; overflow-y: auto; background: var(--canvas); }
.settings-header { display: flex; align-items: flex-start; gap: 18px; padding: 32px max(32px, calc((100% - 960px) / 2)); border-bottom: 1px solid var(--line); background: var(--bg); }
.settings-header .icon-button { margin-top: 25px; border: 1px solid var(--line); }
.settings-eyebrow { margin: 0 0 8px; color: var(--muted); font-size: 11px; letter-spacing: .6px; }
h1 { margin: 0; font-size: 26px; font-weight: 600; letter-spacing: -.6px; }
.settings-description { margin: 8px 0 0; color: var(--muted); font-size: 13px; }
.settings-content { width: min(960px, calc(100% - 64px)); margin: 32px auto 48px; }
@media (max-width: 600px) {
  .settings-header { padding: 24px 20px; gap: 12px; }
  .settings-content { width: calc(100% - 32px); margin-top: 20px; }
  h1 { font-size: 23px; }
}
</style>
