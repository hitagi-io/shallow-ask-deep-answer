<script setup lang="ts">
import { computed, ref } from 'vue'
import type { useKnowledge } from '../composables/useKnowledge'
import type { useProjects } from '../composables/useProjects'
import AppIcon from './AppIcon.vue'
import AppNotice from './AppNotice.vue'
import AppDialog from './AppDialog.vue'
import { knowledgeFileAccept, knowledgeFileTypes } from '../utils/knowledgeFormats'
const props = defineProps<{ knowledge: ReturnType<typeof useKnowledge>; projects: ReturnType<typeof useProjects> }>()
const emit = defineEmits<{ back: []; ask: [id: string]; deleted: [id: string] }>()
const { files, knowledgeLoading, knowledgeBuilding, knowledgeUploading, knowledgeMsg, knowledgeMsgType, dragOver, fileInput, deletingName, health, healthChecking, healthRepairing, knowledgeBusy, loadKnowledgeState, onFileSelect, onDrop, handleDeleteFile, handleBuild, handleHealthCheck, handleHealthRepair, formatHealthTime } = props.knowledge
const { filteredFiles, search, statusFilter, uploadProgress, uploadCompleted, loadError, selectFiles, clearNotice } = props.knowledge
const { healthNotice, documentStatus, indexStatus } = props.knowledge
const { uploadTasks, indexing, taskLoadError, taskCompleted, taskTotal, taskFileDetail } = props.knowledge
const { projects: projectList, selectedId, selected, loading, saving, error, load, save, remove } = props.projects
const projectSearch = ref('')
const filteredProjects = computed(() => projectList.value.filter(project => project.name.toLocaleLowerCase().includes(projectSearch.value.trim().toLocaleLowerCase())))
const dialog = ref<'create' | 'rename' | null>(null)
const projectName = ref('')
const busy = computed(() => knowledgeBusy.value || saving.value || loading.value)
function openDialog(type: 'create' | 'rename') {
  error.value = ''
  projectName.value = type === 'rename' ? selected.value?.name || '' : ''
  dialog.value = type
}
async function submitProject() {
  if (!dialog.value) return
  if (await save(projectName.value, dialog.value === 'rename' ? selectedId.value || undefined : undefined)) dialog.value = null
}
async function removeProject() {
  const id = selectedId.value
  if (id && await remove(id)) emit('deleted', id)
}
</script>

<template>
  <div class="knowledge-page">
    <header class="knowledge-header">
      <button class="icon-button btn-back" @click="emit('back')" aria-label="返回对话" title="返回对话">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M19 12H5M12 19l-7-7 7-7"/></svg>
      </button>
      <div class="knowledge-title"><div class="eyebrow">工作空间 / 知识库</div><h1>让资料成为你的知识</h1><p>按项目整理文档，让每一次提问都有据可循。</p></div>
      <span class="private-label"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3m-4 5v2"/></svg>仅自己可见</span>
    </header>
    <AppNotice v-if="error && !dialog" class="knowledge-notice" tone="error" dismissible @dismiss="error = ''">{{ error }} <button v-if="!saving" class="text-action" @click="load">重新加载</button></AppNotice>
    <div class="knowledge-shell">
      <aside class="project-panel" aria-label="知识库项目">
        <div class="panel-heading"><h2>项目 <span>{{ projectList.length }}</span></h2><button class="icon-button" :disabled="busy" @click="openDialog('create')" aria-label="新建项目" title="新建项目"><AppIcon name="plus" :size="17" /></button></div>
        <input v-if="projectList.length > 4 || projectSearch" v-model="projectSearch" class="filter-input project-search" type="search" placeholder="搜索项目" aria-label="搜索项目" />
        <p v-if="loading && !projectList.length" class="panel-empty" role="status">正在加载项目…</p>
        <p v-else-if="!projectList.length" class="panel-empty">按工作主题建立项目<br />将资料整理在一起</p>
        <p v-else-if="!filteredProjects.length" class="panel-empty">没有匹配的项目</p>
        <nav v-else class="project-list" aria-label="选择知识库项目">
          <button v-for="project in filteredProjects" :key="project.id" :class="{ selected: selectedId === project.id }" :aria-current="selectedId === project.id ? 'true' : undefined" :title="project.name" :disabled="busy" @click="selectedId = project.id">
            <AppIcon name="folder" :size="17" /><span>{{ project.name }}</span><span v-if="selectedId === project.id" class="selected-dot"></span>
          </button>
        </nav>
        <button class="new-project" :disabled="busy" @click="openDialog('create')"><AppIcon name="plus" :size="15" />新建项目</button>
        <div class="panel-footer"><AppIcon name="book" :size="18" /><p>一个项目，一个知识主题。<br />问答聚焦于所选项目的资料。</p></div>
      </aside>

      <section v-if="!selected" class="project-empty">
        <div class="folder-tile"><AppIcon name="folder" :size="30" /></div>
        <h2>{{ projectList.length ? '选择一个项目' : '从第一个项目开始' }}</h2>
        <p>{{ projectList.length ? '查看项目资料，或创建一个新的知识主题。' : '为工作资料建立专属空间，上传文档后即可开始问答。' }}</p>
        <button class="btn btn-primary" :disabled="saving" @click="openDialog('create')"><AppIcon name="plus" :size="16" />新建项目</button>
      </section>
      <div v-else class="project-content">
        <header class="project-heading">
          <div class="project-title"><span class="eyebrow">项目空间</span><h2>{{ selected.name }}</h2></div>
          <div class="project-actions">
            <button class="btn btn-secondary btn-small" :disabled="busy" @click="openDialog('rename')">重命名</button>
            <button class="btn btn-secondary btn-danger btn-small" :disabled="busy" @click="removeProject">删除项目</button>
            <button class="btn btn-primary btn-small" :disabled="busy" @click="emit('ask', selected.id)">开始问答<AppIcon name="arrow-up-right" :size="14" /></button>
          </div>
        </header>
        <AppNotice v-if="knowledgeMsg" class="knowledge-notice" :tone="knowledgeMsgType === 'error' ? 'error' : 'success'" dismissible @dismiss="clearNotice">{{ knowledgeMsg }}</AppNotice>
        <div class="project-stats" aria-label="项目概览" :aria-busy="knowledgeLoading">
          <div><span><AppIcon name="file" :size="15" />项目文档</span><strong>{{ files.length.toString().padStart(2, '0') }}<small>份</small></strong></div>
          <div><span><AppIcon name="book" :size="15" />已入库</span><strong>{{ files.filter(file => file.indexed).length.toString().padStart(2, '0') }}<small>份</small></strong></div>
          <div><span><AppIcon name="success" :size="15" />资料状态</span><strong class="status-value" :class="documentStatus.tone"><span class="status-dot"></span>{{ documentStatus.label }}</strong></div>
        </div>
        <p v-if="healthNotice" class="health-notice" role="status">{{ healthNotice }}</p>
        <div class="dropzone" role="button" :tabindex="busy ? -1 : 0" :aria-disabled="busy" :aria-busy="knowledgeUploading" aria-label="选择文件上传到当前项目" @keydown.enter.prevent="!busy && selectFiles()" @keydown.space.prevent="!busy && selectFiles()" :class="{ over: dragOver, busy }" @dragover.prevent="dragOver = !busy" @dragleave="dragOver = false" @drop.prevent="!busy && onDrop($event)" @click="!busy && selectFiles()">
          <input ref="fileInput" type="file" :accept="knowledgeFileAccept" :disabled="busy" multiple hidden @change="onFileSelect" />
          <span class="upload-icon"><AppIcon name="upload" :size="22" /></span>
          <div><strong>{{ knowledgeUploading ? '正在上传资料…' : '拖拽文档至此，或点击上传' }}</strong><p>{{ knowledgeFileTypes }}</p><p>图片与扫描 PDF 自动识别文字 · 保存后在后台入库</p></div>
          <span class="upload-label"><AppIcon name="plus" :size="14" />选择文件</span>
        </div>
        <section v-if="uploadProgress.length" class="upload-progress" aria-label="上传进度" aria-live="polite">
          <p class="hint">已处理 {{ uploadCompleted }} / {{ uploadCompleted + uploadProgress.length }} 个文件</p>
          <div v-for="(result, i) in uploadProgress" :key="i" class="progress-row"><span>{{ result.name }}</span><small>{{ result.detail }}</small></div>
        </section>

        <section v-if="uploadTasks.length || taskLoadError" class="upload-progress" aria-label="后台入库任务">
          <div class="section-heading"><h3>最近上传</h3><span class="hint" role="status">已处理 {{ taskCompleted }} / {{ taskTotal }} 个文件{{ indexing ? ' · 后台处理中' : '' }}</span></div>
          <p v-if="taskLoadError" class="health-notice" role="alert">{{ taskLoadError }}</p>
          <p v-if="indexing" class="hint">可以继续上传、切换项目或关闭页面；回到此处即可查看结果。</p>
          <div v-for="task in uploadTasks" :key="task.task_id" class="upload-task">
            <div v-for="(file, index) in task.files" :key="index" class="progress-row" :class="{ 'task-error': file.status === 'error' }">
              <span>{{ file.name }}</span><small>{{ taskFileDetail(file) }}</small>
            </div>
            <p v-if="task.error" class="health-notice">{{ task.error }}</p>
          </div>
        </section>

        <section class="documents-section" aria-labelledby="documents-title" :aria-busy="knowledgeLoading">
          <div class="section-heading"><h3 id="documents-title">项目资料 <span>{{ files.length }}</span></h3><div class="section-actions">
            <button class="btn btn-secondary btn-small" :disabled="busy" @click="loadKnowledgeState">{{ knowledgeLoading ? '刷新中…' : '刷新' }}</button>
            <button class="btn btn-secondary btn-small" :disabled="busy || indexing" @click="handleBuild">{{ knowledgeBuilding ? '补建中…' : '补建索引' }}</button>
          </div></div>
          <div class="file-filters"><input v-model="search" class="filter-input" placeholder="搜索文档名称" aria-label="搜索文档名称" type="search" /><select v-model="statusFilter" class="filter-input" aria-label="入库状态"><option value="all">全部状态</option><option value="indexed">已入库</option><option value="pending">未入库</option></select></div>
          <div v-if="loadError" class="document-empty" role="alert">{{ loadError }} <button class="text-action" :disabled="busy" @click="loadKnowledgeState">重新加载</button></div>
          <div v-else-if="knowledgeLoading && !files.length" class="document-empty" role="status"><span class="loading-indicator" aria-hidden="true"></span>正在加载资料…</div>
          <div v-else-if="!filteredFiles.length" class="document-empty"><span class="empty-file-icon"><AppIcon name="file" :size="24" /></span><strong>{{ files.length ? '没有匹配的文档' : '这里将存放你的项目资料' }}</strong><p>{{ files.length ? '试试其他关键词，或调整入库状态筛选。' : '上传第一份文档，让知识问答从这里开始。' }}</p></div>
          <ul v-else class="file-list">
            <li v-for="file in filteredFiles" :key="file.name">
              <span class="file-icon"><AppIcon name="file" :size="20" /></span><div class="file-info"><span class="file-name">{{ file.name }}</span><small>{{ file.name.split('.').pop()?.toUpperCase() }} 文档</small></div>
              <span class="file-status" :class="file.indexed ? 'indexed' : 'pending'">{{ file.indexed ? '已入库' : '待入库' }}</span>
              <button class="btn btn-danger btn-small" :disabled="busy || indexing" :aria-label="`删除文档：${file.name}`" @click="handleDeleteFile(file.name)">{{ deletingName === file.name ? '删除中…' : '删除' }}</button>
            </li>
          </ul>
        </section>
        <details class="health-section">
          <summary><svg class="health-chevron" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 5 7 7-7 7"/></svg><span>索引维护</span><span class="health-indicator" :class="indexStatus.tone"><span class="status-dot"></span>{{ indexStatus.label }}</span></summary>
          <div class="health-content">
            <div class="section-heading"><p class="hint">最近检查：{{ formatHealthTime(health?.checked_at || null) }}<br />{{ health?.stats.chunks || 0 }} 个资料片段</p><div class="section-actions">
              <button class="btn btn-secondary btn-small" :disabled="busy || indexing" @click="handleHealthCheck">{{ healthChecking ? '检查中…' : '检查索引' }}</button>
              <button class="btn btn-secondary btn-small" :disabled="busy || indexing" @click="handleHealthRepair">{{ healthRepairing ? '修复中…' : '修复索引' }}</button>
            </div></div>
            <ul v-if="health?.issues.length" class="health-issues"><li v-for="(issue, i) in health.issues" :key="i"><span class="issue-severity" :class="issue.severity">{{ issue.severity === 'error' ? '异常' : '提醒' }}</span><div>{{ issue.title }}（{{ issue.count }}）<p v-if="issue.detail">{{ issue.detail }}</p></div></li></ul>
          </div>
        </details>
      </div>
    </div>
  </div>
  <AppDialog v-if="dialog" :title="dialog === 'create' ? '新建项目' : '重命名项目'" compact @close="!saving && (dialog = null)">
    <form @submit.prevent="submitProject" class="project-form" autocomplete="off">
      <label for="project-name">项目名称</label><input id="project-name" v-model="projectName" class="filter-input" autocomplete="off" autofocus maxlength="80" required placeholder="例如：产品研发、客户资料" :disabled="saving" aria-describedby="project-name-hint" /><p id="project-name-hint" class="form-hint">用一个清晰的名称，为相关资料建立共同的主题。</p>
      <AppNotice v-if="error" tone="error">{{ error }}</AppNotice>
      <div class="dialog-actions"><button type="button" class="btn btn-secondary" :disabled="saving" @click="dialog = null">取消</button><button class="btn btn-primary" type="submit" :disabled="saving || !projectName.trim()">{{ saving ? '保存中…' : dialog === 'create' ? '创建项目' : '保存' }}</button></div>
    </form>
  </AppDialog>
</template>

<style scoped>
.knowledge-page { flex: 1; min-height: 0; overflow-y: auto; padding: 40px; background: var(--canvas); }
.knowledge-page > * { max-width: 1320px; margin-left: auto; margin-right: auto; }
.knowledge-header { display: flex; align-items: center; gap: 20px; margin-bottom: 32px; }
.knowledge-title { min-width: 0; }
.btn-back { width: 40px; height: 40px; border: 1px solid var(--line); border-radius: var(--radius-control); background: var(--bg); box-shadow: var(--shadow-sm); }
.eyebrow { font-size: 12px; font-weight: 500; letter-spacing: .06em; color: var(--muted); }
.knowledge-header h1 { font-size: 26px; font-weight: 650; line-height: 1.4; margin: 8px 0 0; letter-spacing: -.6px; }
.knowledge-title > p { font-size: 13px; line-height: 1.7; color: var(--muted); margin: 8px 0 0; }
.private-label { display: inline-flex; align-items: center; gap: 7px; flex-shrink: 0; margin-left: auto; padding: 8px 12px; border: 1px solid var(--line); border-radius: 999px; background: var(--bg); font-size: 12px; color: var(--muted); }
.knowledge-shell { display: grid; grid-template-columns: 232px minmax(0, 1fr); border: 1px solid var(--line); border-radius: var(--radius-panel); min-height: 650px; overflow: hidden; background: var(--bg); box-shadow: var(--shadow-sm); }
.project-panel { min-width: 0; padding: 24px 16px; border-right: 1px solid var(--line); background: var(--sidebar); display: flex; flex-direction: column; }
.panel-heading, .section-heading { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
.panel-heading { padding: 0 8px; margin-bottom: 16px; }
.panel-heading h2, .section-heading h3 { display: flex; align-items: center; gap: 8px; flex-shrink: 0; font-size: 14px; font-weight: 650; margin: 0; }
.panel-heading h2 span, .section-heading h3 span { display: inline-grid; place-items: center; min-width: 23px; height: 23px; padding: 0 6px; border-radius: 6px; font-size: 11px; font-variant-numeric: tabular-nums; font-weight: 500; color: var(--muted); background: var(--hover); }
.project-search { width: 100%; margin-bottom: 12px; }
.project-list { display: flex; flex-direction: column; gap: 6px; }
.project-list button { display: flex; align-items: center; gap: 10px; min-height: 44px; text-align: left; padding: 10px 12px; border: 1px solid transparent; border-radius: var(--radius-control); color: var(--muted); font-size: 13px; transition: background-color var(--duration-fast), color var(--duration-fast), border-color var(--duration-fast); }
.project-list button:hover:not(:disabled) { background: var(--hover); color: var(--text); }
.project-list button.selected { background: var(--bg); border-color: var(--accent-border); color: var(--accent); font-weight: 600; box-shadow: var(--shadow-sm); }
.project-list button > span:not(.selected-dot) { min-width: 0; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.selected-dot { flex-shrink: 0; margin-left: auto; width: 6px; height: 6px; background: var(--accent); border-radius: 50%; }
.panel-empty { font-size: 13px; color: var(--muted); line-height: 1.9; padding: 12px 8px; }
.new-project { display: flex; align-items: center; gap: 9px; font-size: 13px; color: var(--accent); min-height: 42px; padding: 10px 12px; margin-top: 12px; border-radius: var(--radius-control); text-align: left; }
.new-project:hover:not(:disabled) { background: var(--accent-soft); }
.panel-footer { margin: auto 8px 0; padding-top: 64px; font-size: 12px; line-height: 1.8; color: var(--muted); }
.panel-footer > .app-icon { color: var(--accent); margin-bottom: 10px; }
.panel-footer p { margin: 0; }
.project-empty { min-height: 560px; display: flex; align-items: center; justify-content: center; flex-direction: column; padding: 40px; text-align: center; }
.folder-tile { display: grid; place-items: center; width: 72px; height: 72px; border: 1px solid var(--accent-border); border-radius: var(--radius-panel); background: var(--accent-soft); color: var(--accent); margin-bottom: 24px; }
.project-empty h2 { font-size: 23px; font-weight: 600; margin: 0; letter-spacing: -.4px; }
.project-empty p { max-width: 340px; color: var(--muted); font-size: 14px; line-height: 1.8; margin: 12px 0 26px; }
.project-content { min-width: 0; padding: 32px; }
.project-heading { display: flex; gap: 20px; align-items: center; justify-content: space-between; margin-bottom: 28px; }
.project-title { min-width: 0; }
.project-title h2 { font-size: 24px; font-weight: 650; line-height: 1.4; letter-spacing: -.5px; margin: 7px 0 0; overflow-wrap: anywhere; }
.project-actions { display: flex; flex-wrap: wrap; gap: 8px; justify-content: flex-end; flex-shrink: 0; }
.project-stats { display: grid; grid-template-columns: 1fr 1fr 1.3fr; gap: 12px; margin-bottom: 24px; }
.project-stats > div { min-width: 0; padding: 18px 20px; border: 1px solid var(--line); border-radius: var(--radius-card); background: var(--canvas); }
.project-stats > div > span { display: flex; align-items: center; gap: 7px; font-size: 12px; color: var(--muted); margin-bottom: 12px; }
.project-stats strong { display: flex; align-items: baseline; gap: 8px; font-size: 28px; line-height: 1.2; font-weight: 600; font-variant-numeric: tabular-nums; letter-spacing: -.8px; }
.project-stats strong > small { font-family: inherit; font-size: 12px; font-weight: 400; color: var(--muted); letter-spacing: 0; }
.project-stats .status-value { display: inline-flex; align-items: center; gap: 7px; font-size: 14px; line-height: 1.5; font-weight: 500; letter-spacing: 0; margin-top: 6px; color: var(--muted); }
.status-dot { flex-shrink: 0; width: 6px; height: 6px; background: currentColor; border-radius: 50%; }
.project-stats .status-value.success, .health-indicator.success { color: var(--success); }
.project-stats .status-value.warning, .health-indicator.warning { color: var(--warning); }
.project-stats .status-value.error, .health-indicator.error { color: var(--danger); }
.health-notice { margin: 0 0 20px; padding: 12px 16px; border: 1px solid var(--warning-border); border-radius: var(--radius-control); color: var(--warning); background: var(--warning-bg); font-size: 13px; line-height: 1.8; overflow-wrap: anywhere; }
.dropzone { display: flex; align-items: center; gap: 16px; background: var(--canvas); border: 1px dashed var(--accent-border); padding: 24px; border-radius: var(--radius-card); cursor: pointer; transition: background-color var(--duration-fast), border-color var(--duration-fast); }
.dropzone:hover:not(.busy), .dropzone.over { background: var(--accent-soft); border-color: var(--accent); }
.dropzone.busy { cursor: wait; opacity: .6; }
.upload-icon { display: grid; place-items: center; flex-shrink: 0; width: 44px; height: 44px; border-radius: 12px; color: var(--accent); background: var(--accent-soft); }
.dropzone strong { font-size: 14px; font-weight: 600; line-height: 1.5; }
.dropzone p { font-size: 12px; color: var(--muted); line-height: 1.7; margin: 6px 0 0; }
.upload-label { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; font-weight: 500; margin-left: auto; white-space: nowrap; border: 1px solid var(--line-strong); padding: 8px 12px; border-radius: var(--radius-control); background: var(--bg); color: var(--accent); }
.documents-section { margin-top: 28px; border: 1px solid var(--line); border-radius: var(--radius-card); padding: 20px; }
.section-actions { display: flex; flex-wrap: wrap; gap: 8px; }
.file-filters { display: flex; gap: 10px; margin: 18px 0 8px; }
.file-filters input { flex: 1; }
.file-filters select { max-width: 124px; }
.document-empty { min-height: 210px; padding: 36px 16px; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 10px; color: var(--muted); font-size: 13px; line-height: 1.7; text-align: center; overflow-wrap: anywhere; }
.document-empty strong { color: var(--text); font-size: 14px; font-weight: 500; }
.document-empty p { margin: 0; }
.empty-file-icon { display: grid; place-items: center; width: 48px; height: 48px; margin-bottom: 4px; color: var(--subtle); background: var(--surface); border-radius: var(--radius-card); }
.loading-indicator { width: 22px; height: 22px; margin-bottom: 4px; border: 2px solid var(--line); border-top-color: var(--accent); border-radius: 50%; animation: spin .8s linear infinite; }
.file-list { list-style: none; margin: 8px 0 0; padding: 0; }
.file-list li { display: flex; align-items: center; gap: 12px; padding: 16px 0; border-bottom: 1px solid var(--line); }
.file-list li:last-child { border-bottom: 0; padding-bottom: 0; }
.file-icon { display: grid; place-items: center; flex-shrink: 0; width: 40px; height: 44px; background: var(--surface); color: var(--accent); border: 1px solid var(--line); border-radius: var(--radius-control); }
.file-info { flex: 1; min-width: 0; }
.file-name { font-size: 13px; font-weight: 500; line-height: 1.6; overflow-wrap: anywhere; }
.file-info small { display: block; margin-top: 4px; font-size: 11px; color: var(--muted); }
.file-status { flex-shrink: 0; font-size: 11px; padding: 4px 8px; border: 1px solid transparent; border-radius: 6px; white-space: nowrap; }
.file-status.indexed { color: var(--success); background: var(--success-bg); border-color: var(--success-border); }
.file-status.pending { color: var(--warning); background: var(--warning-bg); border-color: var(--warning-border); }
.knowledge-notice { margin-bottom: 20px; }
.upload-progress { margin-top: 16px; padding: 12px 16px; background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius-control); }
.progress-row { display: flex; justify-content: space-between; gap: 12px; font-size: 12px; line-height: 1.7; padding: 8px 0; overflow-wrap: anywhere; }
.progress-row > span { min-width: 0; }
.progress-row small { color: var(--muted); text-align: right; }
.progress-row.task-error small { color: var(--danger); }
.upload-task + .upload-task { border-top: 1px solid var(--line); }
.hint { font-size: 12px; line-height: 1.8; color: var(--muted); margin: 0; }
.health-section { margin-top: 20px; border: 1px solid var(--line); border-radius: var(--radius-card); background: var(--canvas); }
.health-section summary { display: flex; align-items: center; gap: 9px; list-style: none; cursor: pointer; padding: 18px 20px; font-size: 13px; border-radius: var(--radius-card); }
.health-section summary::-webkit-details-marker { display: none; }
.health-section summary:hover { background: var(--surface); }
.health-chevron { flex-shrink: 0; color: var(--muted); transition: transform var(--duration-fast); }
.health-section[open] .health-chevron { transform: rotate(90deg); }
.health-indicator { display: inline-flex; align-items: center; gap: 6px; margin-left: auto; font-size: 12px; color: var(--muted); text-align: right; }
.health-content { padding: 0 20px 20px; }
.health-content > .section-heading { padding-top: 16px; border-top: 1px solid var(--line); }
.health-issues { margin: 16px 0 0; padding: 0; list-style: none; }
.health-issues li { display: flex; align-items: baseline; gap: 10px; padding: 12px 0; border-top: 1px solid var(--line); font-size: 12px; line-height: 1.7; }
.health-issues li > div { min-width: 0; overflow-wrap: anywhere; }
.health-issues p { margin: 4px 0 0; color: var(--muted); overflow-wrap: anywhere; }
.issue-severity { font-size: 11px; color: var(--warning); flex-shrink: 0; padding: 2px 6px; border-radius: 4px; background: var(--warning-bg); }
.issue-severity.error { color: var(--danger); background: var(--danger-bg); }
.project-form { display: flex; flex-direction: column; gap: 10px; }
.project-form label { font-size: 13px; font-weight: 500; }
.project-form input { width: 100%; }
.form-hint { margin: 0; font-size: 12px; line-height: 1.7; color: var(--muted); }
.dialog-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 16px; }
@media (max-width: 1100px) {
  .knowledge-page { padding: 32px 24px; }
  .knowledge-shell { grid-template-columns: 200px minmax(0, 1fr); }
  .project-content { padding: 24px; }
  .project-heading { align-items: flex-start; flex-direction: column; gap: 16px; }
  .project-actions { justify-content: flex-start; }
  .project-stats > div { padding: 16px; }
  .dropzone { padding: 20px; }
  .upload-label { display: none; }
}
@media (max-width: 760px) {
  .knowledge-page { padding: 24px 16px; }
  .knowledge-header { align-items: flex-start; flex-wrap: wrap; gap: 12px; margin-bottom: 24px; }
  .knowledge-title { flex: 1; }
  .knowledge-header h1 { font-size: 21px; }
  .knowledge-title > p { font-size: 12px; }
  .private-label { margin-left: 52px; padding: 5px 9px; font-size: 11px; }
  .knowledge-shell { grid-template-columns: minmax(0, 1fr); min-height: 0; }
  .project-panel { border-right: 0; border-bottom: 1px solid var(--line); padding: 16px; }
  .panel-heading { padding: 0; margin-bottom: 10px; }
  .project-list { flex-direction: row; overflow-x: auto; padding-bottom: 4px; }
  .project-list button { flex-shrink: 0; max-width: 220px; }
  .new-project { align-self: flex-start; padding: 8px 10px; margin-top: 8px; }
  .panel-footer { display: none; }
  .panel-empty { padding: 0; margin: 8px 0; }
  .project-content { padding: 24px 16px; }
  .project-title h2 { font-size: 22px; }
  .project-stats { gap: 8px; }
  .project-stats > div { padding: 14px 12px; }
  .project-stats > div > span { font-size: 11px; gap: 5px; }
  .project-stats strong { font-size: 25px; }
  .project-stats .status-value { font-size: 12px; }
  .project-empty { min-height: 380px; padding: 32px 20px; }
  .project-empty h2 { font-size: 21px; }
  .dropzone { padding: 18px 14px; gap: 12px; }
  .dropzone strong { font-size: 13px; }
  .dropzone p { font-size: 11px; }
  .upload-icon { width: 36px; height: 40px; }
  .documents-section { padding: 16px; }
  .section-heading { flex-wrap: wrap; }
  .section-actions { gap: 6px; }
  .file-list li { flex-wrap: wrap; gap: 8px; }
  .file-icon { width: 32px; height: 36px; }
  .file-info { flex-basis: calc(100% - 40px); }
  .file-status { margin-left: 40px; }
  .file-list .btn { margin-left: auto; }
  .health-section summary { padding: 16px; }
  .health-content .section-heading { align-items: flex-start; flex-direction: column; }
  .health-content { padding: 0 16px 16px; }
}
@media (max-width: 380px) {
  .knowledge-page { padding: 20px 10px; }
  .project-content { padding: 20px 12px; }
  .project-stats { grid-template-columns: 1fr 1fr; }
  .project-stats > div:last-child { grid-column: 1 / -1; display: flex; justify-content: space-between; align-items: center; gap: 8px; }
  .project-stats > div:last-child > span, .project-stats .status-value { margin: 0; }
  .project-actions { gap: 6px; }
  .project-actions .btn { padding-left: 9px; padding-right: 9px; }
  .documents-section { padding: 14px 12px; }
  .file-filters { gap: 6px; }
}
</style>
