<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import type { useKnowledge } from '../composables/useKnowledge'
import type { useProjects } from '../composables/useProjects'
import AppIcon from '../components/common/AppIcon.vue'
import AppNotice from '../components/common/AppNotice.vue'
import AppDialog from '../components/common/AppDialog.vue'
import AppProgress from '../components/common/AppProgress.vue'
import PageHeader from '../components/layout/PageHeader.vue'
import { knowledgeFileAccept, knowledgeFileTypes } from '../utils/knowledgeFormats'
import { fileSortLabels, type FileSort } from '../utils/knowledgeFiles'
const props = defineProps<{ knowledge: ReturnType<typeof useKnowledge>; projects: ReturnType<typeof useProjects> }>()
const emit = defineEmits<{ back: []; ask: [id: string]; deleted: [id: string] }>()
const { files, knowledgeLoading, knowledgeBuilding, knowledgeUploading, knowledgeMsg, knowledgeMsgType, dragOver, fileInput, deletingName, health, healthChecking, healthRepairing, knowledgeBusy, loadKnowledgeState, onFileSelect, onDrop, handleDeleteFile, handleBuild, handleHealthCheck, handleHealthRepair, formatHealthTime } = props.knowledge
const { fileTotal, indexedTotal, matchedFiles, filePage, filePageCount, filesLoading, goToFilePage, search, statusFilter, fileSort, uploadProgress, loadError, selectFiles, clearNotice } = props.knowledge
const { healthNotice, documentStatus, indexStatus } = props.knowledge
const { indexing, taskLoadError } = props.knowledge
const { projects: projectList, selectedId, selected, loading, saving, error, load, save, remove } = props.projects
const projectSearch = ref('')
const filteredProjects = computed(() => projectList.value.filter(project => project.name.toLocaleLowerCase().includes(projectSearch.value.trim().toLocaleLowerCase())))
const dialog = ref<'create' | 'rename' | null>(null)
const projectName = ref('')
const busy = computed(() => knowledgeBusy.value || saving.value || loading.value)
const sortControl = ref<HTMLElement | null>(null)
const sortTrigger = ref<HTMLButtonElement | null>(null)
const sortMenuOpen = ref(false)
function closeSortMenu(restoreFocus = false) {
  sortMenuOpen.value = false
  if (restoreFocus) sortTrigger.value?.focus()
}
async function toggleSortMenu() {
  sortMenuOpen.value = !sortMenuOpen.value
  if (sortMenuOpen.value) {
    await nextTick()
    sortControl.value?.querySelector<HTMLButtonElement>('[aria-pressed="true"]')?.focus()
  }
}
function chooseFileSort(value: FileSort) {
  fileSort.value = value
  closeSortMenu(true)
}
function onSortOutsideClick(event: MouseEvent) {
  if (event.target instanceof Node && !sortControl.value?.contains(event.target)) closeSortMenu()
}
function onSortFocusOut(event: FocusEvent) {
  if (!(event.relatedTarget instanceof Node) || !sortControl.value?.contains(event.relatedTarget)) closeSortMenu()
}
watch([selectedId, busy], () => closeSortMenu())
onMounted(() => document.addEventListener('click', onSortOutsideClick))
onUnmounted(() => document.removeEventListener('click', onSortOutsideClick))
const documentsSection = ref<HTMLElement | null>(null)
const pageInput = ref<number | string>(filePage.value)
watch([filePage, filePageCount, selectedId], () => {
  pageInput.value = filePage.value
})
function submitFilePage() {
  if (typeof pageInput.value === 'number') return changeFilePage(pageInput.value)
}
async function changeFilePage(page: number) {
  if (page === filePage.value) return
  const projectId = selectedId.value
  await goToFilePage(page)
  await nextTick()
  if (selectedId.value === projectId && !loadError.value && !filesLoading.value) {
    documentsSection.value?.scrollIntoView({ block: 'start' })
  }
}
const canAsk = computed(() => !busy.value && !indexing.value && !loadError.value && !taskLoadError.value && indexedTotal.value > 0)
const askTitle = computed(() => canAsk.value ? '开始问答'
  : indexing.value ? '入库中，完成后可提问'
  : busy.value ? '正在处理，请稍候'
  : loadError.value || taskLoadError.value ? '资料状态读取失败，请刷新'
  : '请先完成文档入库')
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
    <PageHeader title="让资料成为你的知识" description="按项目整理文档，让每一次提问都有据可循。" eyebrow="工作空间 / 知识库" back-label="返回对话" @back="emit('back')">
      <span class="private-label"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3m-4 5v2"/></svg>仅自己可见</span>
    </PageHeader>
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
        <button class="btn new-project" :disabled="busy" @click="openDialog('create')"><AppIcon name="plus" :size="16" />新建项目</button>
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
            <button class="btn btn-primary btn-small" :disabled="!canAsk" :title="askTitle" aria-describedby="project-upload-hint" @click="canAsk && emit('ask', selected.id)">{{ indexing ? '入库中…' : '开始问答' }}<AppIcon name="arrow-up-right" :size="14" /></button>
          </div>
        </header>
        <AppNotice v-if="knowledgeMsg" class="knowledge-notice" :tone="knowledgeMsgType === 'error' ? 'error' : 'success'" dismissible @dismiss="clearNotice">{{ knowledgeMsg }}</AppNotice>
        <div class="project-stats" aria-label="项目概览" :aria-busy="knowledgeLoading">
          <div><span><AppIcon name="file" :size="15" />项目文档</span><strong>{{ fileTotal.toString().padStart(2, '0') }}<small>份</small></strong></div>
          <div><span><AppIcon name="book" :size="15" />已入库</span><strong>{{ indexedTotal.toString().padStart(2, '0') }}<small>份</small></strong></div>
          <div><span><AppIcon name="success" :size="15" />资料状态</span><strong class="status-value" :class="documentStatus.tone"><span class="status-dot"></span>{{ documentStatus.label }}</strong></div>
        </div>
        <p v-if="healthNotice" class="health-notice" role="status">{{ healthNotice }}</p>
        <div class="dropzone" role="button" :tabindex="busy ? -1 : 0" :aria-disabled="busy" :aria-busy="knowledgeUploading" aria-label="选择文件上传到当前项目" @keydown.enter.prevent="!busy && selectFiles()" @keydown.space.prevent="!busy && selectFiles()" :class="{ over: dragOver, busy }" @dragover.prevent="dragOver = !busy" @dragleave="dragOver = false" @drop.prevent="!busy && onDrop($event)" @click="!busy && selectFiles()">
          <input ref="fileInput" type="file" :accept="knowledgeFileAccept" :disabled="busy" multiple hidden @change="onFileSelect" />
          <span class="upload-icon"><AppIcon name="upload" :size="22" /></span>
          <div><strong>{{ knowledgeUploading ? '正在上传资料…' : '拖拽文档至此，或点击上传' }}</strong><p>{{ knowledgeFileTypes }}</p><p id="project-upload-hint">图片与扫描 PDF 自动识别文字 · 入库完成后可用于回答</p></div>
          <span class="upload-label"><AppIcon name="plus" :size="14" />选择文件</span>
        </div>
        <section v-if="uploadProgress.length || taskLoadError" class="upload-progress" aria-label="上传和入库进度">
          <div class="section-heading"><h3>最近上传</h3><span class="hint upload-history-hint">最近 7 条，新上传在前；有未完成记录时，总数可能超过 7 条。</span></div>
          <p v-if="taskLoadError" class="health-notice" role="alert">{{ taskLoadError }}</p>
          <div v-for="task in uploadProgress" :key="task.id" class="upload-task">
            <div v-for="(file, index) in task.files" :key="index" class="upload-file">
              <div class="progress-row" :class="{ 'task-error': file.status === 'error' }">
                <span>{{ file.name }}</span>
                <small class="upload-file-status"><span role="status">{{ file.detail }}</span><span v-if="file.status !== 'success' && file.status !== 'error'" class="upload-percent">{{ file.percent }}%</span></small>
              </div>
              <AppProgress v-if="file.status !== 'success' && file.status !== 'error'" :value="file.percent" :label="`${file.name} 的上传和入库进度`" :detail="file.detail" />
            </div>
            <p v-if="task.error" class="health-notice">{{ task.error }}</p>
          </div>
        </section>

        <details class="health-section">
          <summary><AppIcon class="health-chevron" name="chevron-right" :size="16" /><span>索引维护</span><span class="health-indicator" :class="indexStatus.tone"><span class="status-dot"></span>{{ indexStatus.label }}</span></summary>
          <div class="health-content">
            <div class="section-heading"><p class="hint">最近检查：{{ formatHealthTime(health?.checked_at || null) }}<br />{{ health?.stats.chunks || 0 }} 个资料片段</p><div class="section-actions">
              <button class="btn btn-secondary btn-small" :disabled="busy || indexing" @click="handleHealthCheck">{{ healthChecking ? '检查中…' : '检查索引' }}</button>
              <button class="btn btn-secondary btn-small" :disabled="busy || indexing" @click="handleHealthRepair">{{ healthRepairing ? '修复中…' : '修复索引' }}</button>
            </div></div>
            <ul v-if="health?.issues.length" class="health-issues"><li v-for="(issue, i) in health.issues" :key="i"><span class="issue-severity" :class="issue.severity">{{ issue.severity === 'error' ? '异常' : '提醒' }}</span><div>{{ issue.title }}（{{ issue.count }}）<p v-if="issue.detail">{{ issue.detail }}</p></div></li></ul>
          </div>
        </details>

        <section ref="documentsSection" class="documents-section" aria-labelledby="documents-title" :aria-busy="filesLoading">
          <div class="section-heading"><h3 id="documents-title">项目资料 <span>{{ fileTotal }}</span></h3><div class="section-actions">
            <button class="btn btn-secondary btn-small" :disabled="busy" @click="loadKnowledgeState">{{ knowledgeLoading ? '刷新中…' : '刷新' }}</button>
            <button class="btn btn-secondary btn-small" :disabled="busy || indexing" @click="handleBuild">{{ knowledgeBuilding ? '补建中…' : '补建索引' }}</button>
          </div></div>
          <div class="file-filters">
            <input v-model="search" class="filter-input" placeholder="搜索全部文档名称" aria-label="搜索文档名称" type="search" maxlength="220" :disabled="busy" />
            <select v-model="statusFilter" class="filter-input" aria-label="入库状态" :disabled="busy"><option value="all">全部状态</option><option value="indexed">已入库</option><option value="pending">未入库</option></select>
            <div ref="sortControl" class="sort-control" @keydown.esc.stop.prevent="closeSortMenu(true)" @focusout="onSortFocusOut">
              <button ref="sortTrigger" class="icon-button icon-button-large sort-trigger" :class="{ changed: fileSort !== 'uploaded_desc' }" :disabled="busy" :aria-expanded="sortMenuOpen" aria-controls="file-sort-options" aria-label="选择资料排序方式" :title="fileSortLabels[fileSort]" @click="toggleSortMenu"><AppIcon name="filter" /></button>
              <div v-if="sortMenuOpen" id="file-sort-options" class="menu-panel sort-menu" role="group" aria-label="资料排序方式">
                <button v-for="(label, value) in fileSortLabels" :key="value" type="button" :disabled="busy" :aria-pressed="fileSort === value" @click="chooseFileSort(value)"><AppIcon class="sort-check" :class="{ selected: fileSort === value }" name="check" :size="16" /><span>{{ label }}</span></button>
              </div>
            </div>
          </div>
          <div v-if="loadError" class="document-empty" role="alert">{{ loadError }} <button class="text-action" :disabled="busy" @click="loadKnowledgeState">重新加载</button></div>
          <div v-else-if="filesLoading && !files.length" class="document-empty" role="status"><span class="loading-spinner" aria-hidden="true"></span>正在加载资料…</div>
          <div v-else-if="!files.length" class="document-empty"><span class="empty-file-icon"><AppIcon name="file" :size="24" /></span><strong>{{ fileTotal ? '没有匹配的文档' : '这里将存放你的项目资料' }}</strong><p>{{ fileTotal ? '试试其他关键词，或调整入库状态筛选。' : '上传第一份文档，让知识问答从这里开始。' }}</p></div>
          <ul v-else class="file-list">
            <li v-for="file in files" :key="file.name">
              <span class="file-icon"><AppIcon name="file" :size="20" /></span><div class="file-info"><span class="file-name">{{ file.name }}</span><small>{{ file.name.split('.').pop()?.toUpperCase() }} 文档</small></div>
              <span class="file-status" :class="file.indexed ? 'indexed' : 'pending'">{{ file.indexed ? '已入库' : '待入库' }}</span>
              <button class="btn btn-danger btn-small" :disabled="busy || indexing" :aria-label="`删除文档：${file.name}`" @click="handleDeleteFile(file.name)">{{ deletingName === file.name ? '删除中…' : '删除' }}</button>
            </li>
          </ul>
          <div v-if="matchedFiles" class="document-pagination">
            <span class="hint" role="status">共 {{ matchedFiles }} 份</span>
            <nav class="pagination-controls" aria-label="项目资料分页">
              <button class="btn btn-secondary btn-small pagination-arrow" :disabled="busy || filesLoading || filePage <= 1" aria-label="上一页" title="上一页" @click="changeFilePage(filePage - 1)"><AppIcon name="arrow-left" :size="16" /></button>
              <form class="page-jump" @submit.prevent="submitFilePage">
                <input v-model="pageInput" class="filter-input page-input" type="number" inputmode="numeric" min="1" :max="filePageCount" step="1" required :disabled="busy || filesLoading" aria-label="页码" title="输入页码，按回车跳转" @blur="pageInput = filePage" />
                <span class="page-total">/ {{ filePageCount }} 页</span>
              </form>
              <button class="btn btn-secondary btn-small pagination-arrow" :disabled="busy || filesLoading || filePage >= filePageCount" aria-label="下一页" title="下一页" @click="changeFilePage(filePage + 1)"><AppIcon name="arrow-right" :size="16" /></button>
            </nav>
          </div>
        </section>
      </div>
    </div>
  </div>
  <AppDialog v-if="dialog" :title="dialog === 'create' ? '新建项目' : '重命名项目'" compact @close="!saving && (dialog = null)">
    <form @submit.prevent="submitProject" class="form-stack" autocomplete="off">
      <label for="project-name">项目名称</label><input id="project-name" v-model="projectName" class="filter-input" autocomplete="off" autofocus maxlength="80" required placeholder="例如：产品研发、客户资料" :disabled="saving" aria-describedby="project-name-hint" /><p id="project-name-hint" class="field-hint">用一个清晰的名称，为相关资料建立共同的主题。</p>
      <AppNotice v-if="error" tone="error">{{ error }}</AppNotice>
      <div class="dialog-actions"><button type="button" class="btn btn-secondary" :disabled="saving" @click="dialog = null">取消</button><button class="btn btn-primary" type="submit" :disabled="saving || !projectName.trim()">{{ saving ? '保存中…' : dialog === 'create' ? '创建项目' : '保存' }}</button></div>
    </form>
  </AppDialog>
</template>

<style scoped>
.knowledge-page { flex: 1; min-height: 0; overflow-y: auto; padding: var(--page-gutter); background: var(--canvas); }
.knowledge-page > * { max-width: var(--page-width); margin-inline: auto; }
.eyebrow { font-size: var(--font-caption); font-weight: 500; letter-spacing: .06em; color: var(--muted); }
.private-label { display: inline-flex; align-items: center; gap: 8px; padding: 6px 12px; border: 1px solid var(--line); border-radius: var(--radius-control); background: var(--bg); font-size: var(--font-caption); line-height: 20px; color: var(--muted); }
.knowledge-shell { display: grid; grid-template-columns: 232px minmax(0, 1fr); border: 1px solid var(--line); border-radius: var(--radius-panel); min-height: 650px; overflow: hidden; background: var(--bg); box-shadow: var(--shadow-sm); }
.project-panel { min-width: 0; padding: 24px 16px; border-right: 1px solid var(--line); background: var(--sidebar); display: flex; flex-direction: column; }
.panel-heading, .section-heading { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
.panel-heading { padding: 0 8px; margin-bottom: 16px; }
.panel-heading h2, .section-heading h3 { display: flex; align-items: center; gap: 8px; flex-shrink: 0; font-size: var(--font-body); font-weight: 600; margin: 0; }
.panel-heading h2 span, .section-heading h3 span { display: inline-grid; place-items: center; min-width: 23px; height: 23px; padding: 0 6px; border-radius: var(--radius-small); font-size: var(--font-caption); font-variant-numeric: tabular-nums; font-weight: 500; color: var(--muted); background: var(--hover); }
.project-search { width: 100%; margin-bottom: 12px; }
.project-list { display: flex; flex-direction: column; gap: 6px; }
.project-list button { display: flex; align-items: center; gap: 8px; min-height: var(--control-height); text-align: left; padding: 9px 12px; border: 1px solid transparent; border-radius: var(--radius-control); color: var(--muted); font-size: var(--font-small); line-height: 20px; transition: background-color var(--duration-fast), color var(--duration-fast), border-color var(--duration-fast); }
.project-list button:hover:not(:disabled) { background: var(--hover); color: var(--text); }
.project-list button.selected { background: var(--bg); border-color: var(--accent-border); color: var(--accent); font-weight: 600; box-shadow: var(--shadow-sm); }
.project-list button > span:not(.selected-dot) { min-width: 0; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.selected-dot { flex-shrink: 0; margin-left: auto; width: 6px; height: 6px; background: var(--accent); border-radius: 50%; }
.panel-empty { font-size: var(--font-small); color: var(--muted); line-height: 1.9; padding: 12px 8px; }
.new-project { justify-content: flex-start; padding-inline: 12px; margin-top: 12px; color: var(--accent); }
.new-project:hover:not(:disabled) { background: var(--accent-soft); }
.panel-footer { margin: auto 8px 0; padding-top: 64px; font-size: var(--font-caption); line-height: 1.8; color: var(--muted); }
.panel-footer > .app-icon { color: var(--accent); margin-bottom: 10px; }
.panel-footer p { margin: 0; }
.project-empty { min-height: 560px; display: flex; align-items: center; justify-content: center; flex-direction: column; padding: 40px; text-align: center; }
.folder-tile { display: grid; place-items: center; width: 72px; height: 72px; border: 1px solid var(--accent-border); border-radius: var(--radius-panel); background: var(--accent-soft); color: var(--accent); margin-bottom: 24px; }
.project-empty h2 { font-size: var(--font-title); font-weight: 600; margin: 0; letter-spacing: -.4px; }
.project-empty p { max-width: 340px; color: var(--muted); font-size: var(--font-body); line-height: 1.8; margin: 12px 0 26px; }
.project-content { min-width: 0; padding: var(--panel-padding); }
.project-heading { display: flex; gap: 20px; align-items: center; justify-content: space-between; margin-bottom: 28px; }
.project-title { min-width: 0; }
.project-title h2 { font-size: var(--font-section); font-weight: 600; line-height: 1.4; margin: 4px 0 0; overflow-wrap: anywhere; }
.project-actions { display: flex; flex-wrap: wrap; gap: 8px; justify-content: flex-end; flex-shrink: 0; }
.project-stats { display: grid; grid-template-columns: 1fr 1fr 1.3fr; gap: 12px; margin-bottom: 24px; }
.project-stats > div { min-width: 0; padding: 18px 20px; border: 1px solid var(--line); border-radius: var(--radius-card); background: var(--canvas); }
.project-stats > div > span { display: flex; align-items: center; gap: 7px; font-size: var(--font-caption); color: var(--muted); margin-bottom: 12px; }
.project-stats strong { display: flex; align-items: baseline; gap: 8px; font-size: 28px; line-height: 1.2; font-weight: 600; font-variant-numeric: tabular-nums; letter-spacing: -.8px; }
.project-stats strong > small { font-family: inherit; font-size: var(--font-caption); font-weight: 400; color: var(--muted); letter-spacing: 0; }
.project-stats .status-value { display: inline-flex; align-items: center; gap: 7px; font-size: var(--font-body); line-height: 1.5; font-weight: 500; letter-spacing: 0; margin-top: 6px; color: var(--muted); }
.status-dot { flex-shrink: 0; width: 6px; height: 6px; background: currentColor; border-radius: 50%; }
.project-stats .status-value.success, .health-indicator.success { color: var(--success); }
.project-stats .status-value.warning, .health-indicator.warning { color: var(--warning); }
.project-stats .status-value.error, .health-indicator.error { color: var(--danger); }
.health-notice { margin: 0 0 20px; padding: 12px 16px; border: 1px solid var(--warning-border); border-radius: var(--radius-control); color: var(--warning); background: var(--warning-bg); font-size: var(--font-small); line-height: 1.8; overflow-wrap: anywhere; }
.dropzone { display: flex; align-items: center; gap: 16px; background: var(--canvas); border: 1px dashed var(--accent-border); padding: 24px; border-radius: var(--radius-card); cursor: pointer; transition: background-color var(--duration-fast), border-color var(--duration-fast); }
.dropzone:hover:not(.busy), .dropzone.over { background: var(--accent-soft); border-color: var(--accent); }
.dropzone.busy { cursor: wait; opacity: .6; }
.upload-icon { display: grid; place-items: center; flex-shrink: 0; width: 44px; height: 44px; border-radius: var(--radius-card); color: var(--accent); background: var(--accent-soft); }
.dropzone strong { font-size: var(--font-body); font-weight: 600; line-height: 1.5; }
.dropzone p { font-size: var(--font-caption); color: var(--muted); line-height: 1.7; margin: 6px 0 0; }
.upload-label { display: inline-flex; align-items: center; gap: 6px; font-size: var(--font-caption); font-weight: 500; margin-left: auto; white-space: nowrap; border: 1px solid var(--line-strong); padding: 8px 12px; border-radius: var(--radius-control); background: var(--bg); color: var(--accent); }
.documents-section { margin-top: var(--section-gap); border: 1px solid var(--line); border-radius: var(--radius-card); padding: var(--panel-padding); }
.section-actions { display: flex; flex-wrap: wrap; gap: 8px; }
.file-filters { display: flex; gap: 8px; margin: 16px 0 8px; }
.file-filters input { flex: 1; }
.file-filters select { max-width: 124px; }
.sort-control { position: relative; flex-shrink: 0; }
.sort-trigger { border: 1px solid var(--line-strong); background: var(--bg); }
.sort-trigger[aria-expanded="true"], .sort-trigger.changed { color: var(--accent); border-color: var(--accent-border); background: var(--accent-soft); }
.sort-check { visibility: hidden; }
.sort-check.selected { visibility: visible; }
.sort-menu { position: absolute; top: calc(100% + 8px); right: 0; z-index: 10; width: 224px; }
.document-empty { min-height: 210px; padding: 36px 16px; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 10px; color: var(--muted); font-size: var(--font-small); line-height: 1.7; text-align: center; overflow-wrap: anywhere; }
.document-empty strong { color: var(--text); font-size: var(--font-body); font-weight: 500; }
.document-empty p { margin: 0; }
.empty-file-icon { display: grid; place-items: center; width: 48px; height: 48px; margin-bottom: 4px; color: var(--subtle); background: var(--surface); border-radius: var(--radius-card); }
.file-list { list-style: none; margin: 8px 0 0; padding: 0; }
.file-list li { display: flex; align-items: center; gap: 12px; padding: 16px 0; border-bottom: 1px solid var(--line); }
.file-list li:last-child { border-bottom: 0; padding-bottom: 0; }
.document-pagination { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 12px; margin-top: 18px; padding-top: 16px; border-top: 1px solid var(--line); }
.pagination-controls { display: flex; align-items: center; gap: 8px; margin-left: auto; font-size: var(--font-small); line-height: 20px; font-variant-numeric: tabular-nums; }
.pagination-arrow, .page-input { min-height: var(--control-small); height: var(--control-small); }
.pagination-arrow { width: var(--control-small); padding: 0; }
.page-jump { display: flex; align-items: center; gap: 6px; white-space: nowrap; }
.page-input { width: 48px; padding: 4px 6px; font: inherit; text-align: center; appearance: textfield; }
.page-total { color: var(--muted); }
.page-input::-webkit-inner-spin-button, .page-input::-webkit-outer-spin-button { appearance: none; margin: 0; }
.file-icon { display: grid; place-items: center; flex-shrink: 0; width: 40px; height: 40px; background: var(--surface); color: var(--accent); border: 1px solid var(--line); border-radius: var(--radius-control); }
.file-info { flex: 1; min-width: 0; }
.file-name { font-size: var(--font-small); font-weight: 500; line-height: 1.6; overflow-wrap: anywhere; }
.file-info small { display: block; margin-top: 4px; font-size: var(--font-caption); color: var(--muted); }
.file-status { flex-shrink: 0; font-size: var(--font-caption); padding: 4px 8px; border: 1px solid transparent; border-radius: var(--radius-small); white-space: nowrap; }
.file-status.indexed { color: var(--success); background: var(--success-bg); border-color: var(--success-border); }
.file-status.pending { color: var(--warning); background: var(--warning-bg); border-color: var(--warning-border); }
.knowledge-notice { margin-bottom: 20px; }
.upload-progress { margin-top: 16px; padding: 12px 16px; background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius-control); }
.progress-row { display: flex; justify-content: space-between; gap: 12px; font-size: var(--font-caption); line-height: 1.7; padding: 8px 0; overflow-wrap: anywhere; }
.progress-row > span { min-width: 0; }
.progress-row small { color: var(--muted); text-align: right; }
.progress-row.task-error small { color: var(--danger); }
.upload-file { padding-bottom: 12px; }
.upload-file-status { display: flex; justify-content: flex-end; align-items: baseline; flex-wrap: wrap; gap: 4px 10px; }
.upload-percent { color: var(--text); font-variant-numeric: tabular-nums; min-width: 4ch; }
.upload-task + .upload-task { border-top: 1px solid var(--line); }
.hint { font-size: var(--font-caption); line-height: 1.8; color: var(--muted); margin: 0; }
.upload-history-hint { margin-left: auto; text-align: right; }
.health-section { margin-top: 20px; border: 1px solid var(--line); border-radius: var(--radius-card); background: var(--canvas); }
.health-section summary { display: flex; align-items: center; gap: 8px; list-style: none; cursor: pointer; padding: 16px var(--panel-padding); font-size: var(--font-small); border-radius: var(--radius-card); }
.health-section summary::-webkit-details-marker { display: none; }
.health-section summary:hover { background: var(--surface); }
.health-chevron { flex-shrink: 0; color: var(--muted); transition: transform var(--duration-fast); }
.health-section[open] .health-chevron { transform: rotate(90deg); }
.health-indicator { display: inline-flex; align-items: center; gap: 6px; margin-left: auto; font-size: var(--font-caption); color: var(--muted); text-align: right; }
.health-content { padding: 0 var(--panel-padding) var(--panel-padding); }
.health-content > .section-heading { padding-top: 16px; border-top: 1px solid var(--line); }
.health-issues { margin: 16px 0 0; padding: 0; list-style: none; }
.health-issues li { display: flex; align-items: baseline; gap: 10px; padding: 12px 0; border-top: 1px solid var(--line); font-size: var(--font-caption); line-height: 1.7; }
.health-issues li > div { min-width: 0; overflow-wrap: anywhere; }
.health-issues p { margin: 4px 0 0; color: var(--muted); overflow-wrap: anywhere; }
.issue-severity { font-size: var(--font-caption); color: var(--warning); flex-shrink: 0; padding: 2px 6px; border-radius: 4px; background: var(--warning-bg); }
.issue-severity.error { color: var(--danger); background: var(--danger-bg); }
@media (max-width: 1100px) {
  .knowledge-shell { grid-template-columns: 200px minmax(0, 1fr); }
  .project-heading { align-items: flex-start; flex-direction: column; gap: 16px; }
  .project-actions { justify-content: flex-start; }
  .project-stats > div { padding: 16px; }
  .dropzone { padding: 20px; }
  .upload-label { display: none; }
}
@media (max-width: 760px) {
  .knowledge-shell { grid-template-columns: minmax(0, 1fr); min-height: 0; }
  .project-panel { border-right: 0; border-bottom: 1px solid var(--line); padding: 16px; }
  .panel-heading { padding: 0; margin-bottom: 10px; }
  .project-list { flex-direction: row; overflow-x: auto; padding-bottom: 4px; }
  .project-list button { flex-shrink: 0; max-width: 220px; }
  .new-project { align-self: flex-start; padding: 8px 10px; margin-top: 8px; }
  .panel-footer { display: none; }
  .panel-empty { padding: 0; margin: 8px 0; }
  .project-stats { gap: 8px; }
  .project-stats > div { padding: 14px 12px; }
  .project-stats > div > span { gap: 4px; }
  .project-stats strong { font-size: 24px; }
  .project-stats .status-value { font-size: var(--font-caption); }
  .project-empty { min-height: 380px; padding: 32px 20px; }
  .dropzone { padding: 18px 14px; gap: 12px; }
  .dropzone strong { font-size: var(--font-small); }
  .upload-icon { width: 36px; height: 40px; }
  .section-heading { flex-wrap: wrap; }
  .section-actions { gap: 6px; }
  .file-filters { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px; }
  .file-filters input { grid-column: 1 / -1; width: 100%; }
  .file-filters select { width: 100%; max-width: none; }
  .upload-history-hint { width: 100%; margin-left: 0; text-align: left; }
  .file-list li { flex-wrap: wrap; gap: 8px; }
  .file-icon { width: 32px; height: 32px; }
  .file-info { flex-basis: calc(100% - 40px); }
  .file-status { margin-left: 40px; }
  .file-list .btn { margin-left: auto; }
  .health-content .section-heading { align-items: flex-start; flex-direction: column; }
}
@media (max-width: 380px) {
  .project-stats { grid-template-columns: 1fr 1fr; }
  .project-stats > div:last-child { grid-column: 1 / -1; display: flex; justify-content: space-between; align-items: center; gap: 8px; }
  .project-stats > div:last-child > span, .project-stats .status-value { margin: 0; }
  .project-actions { gap: 6px; }
  .project-actions .btn { padding-left: 9px; padding-right: 9px; }
  .file-filters { gap: 6px; }
}
</style>
