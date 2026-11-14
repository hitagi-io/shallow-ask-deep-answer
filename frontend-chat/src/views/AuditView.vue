<script setup lang="ts">
import SettingsPage from '../components/layout/SettingsPage.vue'
import AuditLog from '../components/common/AuditLog.vue'

const emit = defineEmits<{ back: [] }>()
</script>

<template>
  <SettingsPage title="操作记录" description="查看当前账号的登录、资料变更、回答和索引处理结果。记录保留 90 天。" @back="emit('back')">
    <AuditLog class="audit-panel" />
  </SettingsPage>
</template>

<style scoped>
.audit-panel { padding: var(--panel-padding); border: 1px solid var(--line); border-radius: var(--radius-card); background: var(--bg); }
</style>
