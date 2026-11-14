<script setup lang="ts">
import AppIcon from '../common/AppIcon.vue'

withDefaults(defineProps<{
  title: string
  description: string
  eyebrow: string
  backLabel?: string
  busy?: boolean
}>(), { backLabel: '返回首页' })
const emit = defineEmits<{ back: [] }>()
</script>

<template>
  <header class="page-header">
    <button type="button" class="icon-button icon-button-large page-back" :disabled="busy" :aria-label="backLabel" :title="backLabel" @click="emit('back')"><AppIcon name="arrow-left" /></button>
    <div class="page-heading"><p class="page-eyebrow">{{ eyebrow }}</p><h1>{{ title }}</h1><p class="page-description">{{ description }}</p></div>
    <div v-if="$slots.default" class="page-header-extra"><slot /></div>
  </header>
</template>

<style scoped>
.page-header { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: 16px; margin-bottom: var(--section-gap); }
.page-back { border: 1px solid var(--line-strong); background: var(--bg); }
.page-heading { min-width: 0; }
.page-eyebrow { margin: 0 0 4px; color: var(--muted); font-size: var(--font-caption); line-height: 20px; }
h1 { margin: 0; font-size: var(--font-title); line-height: 1.4; font-weight: 600; letter-spacing: -.4px; overflow-wrap: anywhere; }
.page-description { margin: 8px 0 0; color: var(--muted); font-size: var(--font-small); line-height: 1.7; }
.page-header-extra { justify-self: end; }
@media (max-width: 760px) {
  .page-header { grid-template-columns: auto minmax(0, 1fr); gap: 12px; }
  .page-header-extra { grid-column: 2; justify-self: start; }
}
</style>
