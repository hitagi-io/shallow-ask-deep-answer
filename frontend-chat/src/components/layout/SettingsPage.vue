<script setup lang="ts">
import PageHeader from './PageHeader.vue'
defineProps<{ title: string; description: string; busy?: boolean }>()
const emit = defineEmits<{ back: [] }>()
</script>

<template>
  <div class="settings-page">
    <PageHeader :title="title" :description="description" :eyebrow="`工作空间 / ${title}`" :busy="busy" @back="emit('back')" />
    <div class="settings-content"><slot /></div>
  </div>
</template>

<style scoped>
.settings-page { flex: 1; min-height: 0; overflow-y: auto; padding: var(--page-gutter); background: var(--canvas); }
.settings-page > * { max-width: var(--settings-width); margin-inline: auto; }
</style>
