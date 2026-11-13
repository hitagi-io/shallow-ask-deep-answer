<script setup lang="ts">
withDefaults(defineProps<{
  name: 'close' | 'plus' | 'arrow-up' | 'arrow-up-right' | 'upload' | 'success' | 'error' | 'folder' | 'book' | 'file'
  size?: number
}>(), { size: 18 })
</script>

<template>
  <svg class="app-icon" :width="size" :height="size" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">
    <path v-if="name === 'close'" d="m6 6 12 12M18 6 6 18" />
    <path v-else-if="name === 'plus'" d="M12 5v14M5 12h14" />
    <path v-else-if="name === 'arrow-up'" d="M12 19V5m-6 6 6-6 6 6" />
    <path v-else-if="name === 'arrow-up-right'" d="M7 17 17 7M7 7h10v10" />
    <path v-else-if="name === 'upload'" d="M12 16V3m-5 5 5-5 5 5M4 16v5h16v-5" />
    <path v-else-if="name === 'folder'" d="M3 7V5a1 1 0 0 1 1-1h5l2 3h9a1 1 0 0 1 1 1v11a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V7Z" />
    <path v-else-if="name === 'book'" d="M12 5v15M3 4h5a4 4 0 0 1 4 2 4 4 0 0 1 4-2h5v15h-5a4 4 0 0 0-4 2 4 4 0 0 0-4-2H3V4Z" />
    <path v-else-if="name === 'file'" d="M14 3H5v18h14V8l-5-5Zm0 0v5h5M8 12h8m-8 4h6" />
    <template v-else>
      <circle cx="12" cy="12" r="9" />
      <path v-if="name === 'success'" d="m8 12 3 3 5-6" />
      <path v-else d="M12 7v6m0 4h.01" />
    </template>
  </svg>
</template>

<style scoped>
.app-icon { display: block; flex-shrink: 0; }
</style>
