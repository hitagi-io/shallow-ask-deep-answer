<script setup lang="ts">
defineProps<{ value: number; label: string; detail: string }>()
</script>

<template>
  <div class="progress-track" role="progressbar" :aria-label="label" aria-valuemin="0" aria-valuemax="100" :aria-valuenow="value" :aria-valuetext="`${detail}，${value}%`">
    <span class="progress-fill" :style="{ width: `${value}%` }"></span>
  </div>
</template>

<style scoped>
.progress-track { height: 8px; overflow: hidden; border-radius: 4px; background: var(--line); }
.progress-fill { display: block; height: 100%; border-radius: inherit; background: var(--success); transition: width var(--duration-fast) linear; }
</style>
