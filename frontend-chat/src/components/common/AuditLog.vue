<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { apiAuditEvents } from '../../api'
import type { AuditEvent } from '../../utils/generation'

const events = ref<AuditEvent[]>([])
const next = ref<number | null>(null)
const loading = ref(false)
const error = ref('')
const actions: Record<string, string> = {
  'auth.login': '登录', 'account.register': '注册账号', 'account.update': '修改账号资料',
  'account.verify_deletion': '验证注销身份', 'account.delete': '注销账号',
  'project.create': '创建项目', 'project.rename': '重命名项目', 'project.delete': '删除项目',
  'source.read': '查看原文', 'file.upload': '上传文件', 'file.upload.start': '开始入库',
  'file.upload.stage': '入库阶段推进', 'file.upload.finish': '入库结果', 'file.delete': '删除文件',
  'file.upload.fail': '入库中断',
  'index.build': '补建索引', 'index.repair': '修复索引', 'upload.recover': '恢复上传',
  'generation.start': '提交问题', 'generation.retry': '重新生成', 'generation.cancel': '停止回答',
  'generation.finish': '回答结果',
}
const results: Record<string, string> = { started: '开始', accepted: '已受理', succeeded: '完成', failed: '失败',
  denied: '未获准', partial: '部分完成', cancelled: '已停止' }
async function load(more = false) {
  if (loading.value) return
  loading.value = true; error.value = ''
  try {
    const page = await apiAuditEvents(more ? next.value : null)
    events.value = more ? [...events.value, ...page.events] : page.events
    next.value = page.next_before
  } catch (e) { error.value = e instanceof Error ? e.message : '无法读取操作记录' }
  finally { loading.value = false }
}
onMounted(() => { void load() })
</script>

<template>
  <div>
    <button class="btn btn-secondary" :disabled="loading" @click="load()">刷新记录</button>
    <p v-if="loading" role="status">正在读取…</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="!loading && !error && !events.length">暂无操作记录。</p>
    <ul class="audit-events">
      <li v-for="event in events" :key="event.id">
        <div>{{ actions[event.action] || event.action }} · {{ results[event.result] || event.result }}<span v-if="event.actor_type === 'system'">（系统任务）</span></div>
        <time :datetime="event.created_at">{{ new Date(event.created_at).toLocaleString() }}</time>
      </li>
    </ul>
    <button v-if="next !== null" class="btn btn-secondary" :disabled="loading" @click="load(true)">更早记录</button>
  </div>
</template>

<style scoped>
.audit-events { list-style: none; padding: 0; max-height: 24rem; overflow: auto; }
.audit-events li { padding: .7rem 0; border-bottom: 1px solid var(--line); }
time { font-size: var(--font-caption); color: var(--muted); }
</style>
