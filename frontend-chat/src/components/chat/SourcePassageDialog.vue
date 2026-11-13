<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { apiSourcePassage, type SourcePassage } from '../../api'
import AppDialog from '../common/AppDialog.vue'

const props = defineProps<{ urls: string[]; projectDeleted: boolean }>()
const emit = defineEmits<{ close: [] }>()
const passage = ref<SourcePassage | null>(null)
const error = ref('')
const loading = ref(false)
const selected = ref(0)
let controller: AbortController | null = null

async function load() {
  controller?.abort()
  controller = null
  passage.value = null
  error.value = ''
  loading.value = false
  if (props.projectDeleted) return
  const request = new AbortController()
  controller = request
  const timeout = window.setTimeout(() => request.abort(), 30000)
  passage.value = null
  error.value = ''
  loading.value = true
  try {
    const result = await apiSourcePassage(props.urls[selected.value], request.signal)
    if (controller === request) passage.value = result
  } catch (err) {
    if (controller === request) error.value = request.signal.aborted
      ? '读取原文超时，请重试'
      : err instanceof Error ? err.message : '无法读取原文，请稍后重试'
  } finally {
    window.clearTimeout(timeout)
    if (controller === request) loading.value = false
  }
}

watch(() => props.urls, () => { selected.value = 0 })
watch(() => [props.urls[selected.value], props.projectDeleted], load, { immediate: true })
onBeforeUnmount(() => { const request = controller; controller = null; request?.abort() })
</script>

<template>
  <AppDialog title="引用原文" @close="emit('close')">
    <nav v-if="urls.length > 1 && !projectDeleted" class="source-navigation" aria-label="相关原文">
      <button type="button" class="btn btn-secondary" :disabled="selected === 0" @click="selected--">上一处</button>
      <span>{{ selected + 1 }} / {{ urls.length }}</span>
      <button type="button" class="btn btn-secondary" :disabled="selected === urls.length - 1" @click="selected++">下一处</button>
    </nav>
    <p v-if="projectDeleted" role="status">来源已删除。所属项目的文档和索引已清理，历史回答仍可查看，原文无法再打开。</p>
    <p v-else-if="loading" role="status">正在定位原文页段…</p>
    <div v-else-if="error" role="alert" class="source-error">
      <p>{{ error }}</p><button type="button" class="btn btn-secondary" @click="load">重试</button>
    </div>
    <template v-else-if="passage">
      <p class="source-name">{{ passage.source }}</p>
      <p class="source-location">{{ passage.location }}</p>
      <p class="source-hint">高亮部分是该引用对应的提取文本，周围保留上下文。位置按文件页序、工作表单元格、幻灯片或文本块标注；段落按提取文本编号。<template v-if="passage.extraction === 'ocr'">此处为 OCR 识别结果，可能存在错字，请结合原始图片核对。</template></p>
      <div class="source-content"><span v-if="passage.clipped_before">…</span>{{ passage.before }}<mark>{{ passage.excerpt }}</mark>{{ passage.after }}<span v-if="passage.clipped_after">…</span></div>
    </template>
  </AppDialog>
</template>

<style scoped>
.source-navigation { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 16px; color: var(--muted); font-size: 13px; }
.source-name { margin: 0 0 8px; font-weight: 600; overflow-wrap: anywhere; }
.source-location { color: var(--accent); margin: 0 0 12px; font-size: 13px; }
.source-hint { color: var(--muted); font-size: 12px; line-height: 1.7; }
.source-content { white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.9; padding: 18px; border: 1px solid var(--line); border-radius: var(--radius-control); }
mark { color: var(--text); background: var(--accent-soft); box-decoration-break: clone; border-bottom: 2px solid var(--accent); }
.source-error { color: var(--danger); }
</style>
