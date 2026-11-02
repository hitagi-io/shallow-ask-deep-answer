<script setup lang="ts">
import type { useKnowledge } from '../composables/useKnowledge'
const props = defineProps<{ knowledge: ReturnType<typeof useKnowledge> }>()
const emit = defineEmits<{ back: [] }>()
const { files, knowledgeLoading, knowledgeBuilding, knowledgeUploading, knowledgeMsg, knowledgeMsgType, dragOver, fileInput, deletingName, health, healthChecking, healthRepairing, knowledgeBusy, loadKnowledgeState, onFileSelect, onDrop, handleDeleteFile, handleBuild, handleHealthCheck, handleHealthRepair, formatHealthTime } = props.knowledge
const { filteredFiles, search, statusFilter, uploadResults, loadError, selectFiles, clearNotice } = props.knowledge
</script>

<template>
  <div class="knowledge-page">
    <header class="knowledge-header">
      <button class="btn-back" @click="emit('back')" title="返回对话">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 12H5M12 19l-7-7 7-7"/></svg>
      </button>
      <div>
        <h1>知识库管理</h1>
        <p class="subtitle">上传文档自动入库，立即可用于检索</p>
      </div>
    </header>

    <div v-if="knowledgeMsg" class="toast" :class="knowledgeMsgType" :role="knowledgeMsgType === 'error' ? 'alert' : 'status'">{{ knowledgeMsg }} <button aria-label="关闭提示" @click="clearNotice">×</button></div>

    <div class="knowledge-cols">
      <div class="knowledge-left">
        <section class="card">
          <h2>上传文件</h2>
          <p class="hint">支持 .txt / .pdf / .docx，上传后自动入库并当场校验。</p>
          <div
            class="dropzone" role="button" :tabindex="knowledgeBusy ? -1 : 0" :aria-disabled="knowledgeBusy" aria-label="选择文件上传" @keydown.enter.prevent="selectFiles" @keydown.space.prevent="selectFiles" :class="{ over: dragOver, busy: knowledgeBusy }"
            @dragover.prevent="dragOver = !knowledgeBusy"
            @dragleave="dragOver = false"
            @drop.prevent="onDrop"
            @click="selectFiles"
            >
            <input ref="fileInput" type="file" accept=".txt,.pdf,.docx" :disabled="knowledgeBusy" multiple hidden @change="onFileSelect" />
            <p v-if="knowledgeUploading">上传并入库中...</p>
            <p v-else-if="knowledgeBusy">其他操作进行中，请稍候...</p>
            <p v-else>点击或拖拽文件到此处</p>
          </div>
          <section v-if="uploadResults.length" class="upload-results" aria-label="上传结果" aria-live="polite">
            <div class="result-header"><strong>上传结果</strong><button v-if="!knowledgeUploading" @click="uploadResults = []">清除</button></div>
            <div v-for="(result, i) in uploadResults" :key="i" class="upload-result" :class="result.status"><span>{{ result.name }}</span><small>{{ result.detail }}</small></div>
          </section>
        </section>

        <section class="card">
          <div class="card-header">
            <h2>索引健康</h2>
            <div class="health-actions">
              <button class="btn-ghost" :disabled="knowledgeBusy" @click="handleHealthCheck">
                <span v-if="healthChecking" class="btn-spinner" aria-hidden="true"></span>
                {{ healthChecking ? '检查中...' : '立即检查' }}
              </button>
              <button
                class="btn-ghost" :disabled="knowledgeBusy" @click="handleHealthRepair"
                title="系统定时自检发现问题会自动修复；此处可立即手动执行一次"
                >
                <span v-if="healthRepairing" class="btn-spinner" aria-hidden="true"></span>
                {{ healthRepairing ? '修复中...' : '手动修复' }}
              </button>
            </div>
          </div>
          <p v-if="!health" class="hint">后台定时自检发现问题会自动修复；以下按钮用于手动检查或立即修复。</p>
          <template v-else>
            <p class="health-summary">
              <span class="health-dot" :class="health.healthy ? 'ok' : 'bad'"></span>
              <strong>{{ health.healthy ? '状态良好' : `发现 ${health.issues.length} 类问题` }}</strong>
              <span class="health-time">最近检查：{{ formatHealthTime(health.checked_at) }}</span>
            </p>
            <p class="hint">磁盘文件 {{ health.stats.files }} 个（已正确入库 {{ health.stats.indexed_files }} 个）· 向量块 {{ health.stats.chunks }}</p>
            <ul v-if="health.issues.length" class="health-issues">
              <li v-for="(iss, i) in health.issues" :key="i">
                <span class="issue-sev" :class="iss.severity">{{ iss.severity === 'error' ? '严重' : '警告' }}</span>
                <span class="issue-text">
                  {{ iss.title }}（{{ iss.count }}）
                  <em v-if="iss.detail" class="issue-detail">{{ iss.detail }}</em>
                </span>
              </li>
            </ul>
          </template>
        </section>
      </div>

      <div class="knowledge-right">
        <section class="card">
          <div class="card-header">
            <h2>文档列表</h2>
            <div class="file-header-actions">
              <button class="btn-ghost" :disabled="knowledgeBusy" @click="loadKnowledgeState">
                <span v-if="knowledgeLoading" class="btn-spinner" aria-hidden="true"></span>
                {{ knowledgeLoading ? '刷新中...' : '刷新' }}
              </button>
              <button
                class="btn-ghost" :disabled="knowledgeBusy" @click="handleBuild"
                title="重新扫描知识库目录，补建未入库文件"
                >
                <span v-if="knowledgeBuilding" class="btn-spinner" aria-hidden="true"></span>
                {{ knowledgeBuilding ? '补建中...' : '补建向量库' }}
              </button>
            </div>
          </div>
          <div class="file-filters"><input v-model="search" class="filter-input" placeholder="搜索文档名称" aria-label="搜索文档名称" type="search" /><select v-model="statusFilter" class="filter-input" aria-label="入库状态"><option value="all">全部状态</option><option value="indexed">已入库</option><option value="pending">未入库</option></select></div>
          <p class="hint">{{ filteredFiles.length }} / {{ files.length }} 个文档</p>
          <div v-if="loadError" role="alert">{{ loadError }} <button class="text-action" :disabled="knowledgeBusy" @click="loadKnowledgeState">重新加载</button></div>
          <div v-else-if="knowledgeLoading && files.length === 0" class="empty" role="status">加载中...</div>
          <div v-else-if="filteredFiles.length === 0" class="empty">{{ files.length ? '没有匹配的文档' : '暂无文档' }}</div>
          <ul v-else class="file-list">
            <li v-for="f in filteredFiles" :key="f.name">
              <span class="file-name">{{ f.name }}</span>
              <span class="file-actions">
                <span class="file-status" :class="f.indexed ? 'indexed' : 'pending'">{{ f.indexed ? '已入库' : '未入库' }}</span>
                <button class="btn-danger" :disabled="knowledgeBusy" @click="handleDeleteFile(f.name)">
                  {{ deletingName === f.name ? '删除中...' : '删除' }}
                </button>
              </span>
            </li>
          </ul>
        </section>
      </div>
    </div>
  </div>
</template>

<style scoped>
.file-filters { display: flex; flex-wrap: wrap; gap: 8px; }
.file-filters input { flex: 1; }
.upload-results { margin-top: 20px; font-size: 12px; }
.result-header { display: flex; justify-content: space-between; }
.upload-result { padding: 10px 0; border-bottom: 1px solid var(--line); overflow-wrap: anywhere; }
.upload-result span, .upload-result small { display: block; line-height: 1.7; }
.upload-result small { color: var(--muted); }
.upload-result.error small { color: var(--danger); }
.upload-result.success small { color: var(--success); }
.knowledge-page { flex: 1; min-height: 0; overflow-y: auto; padding: 72px 36px 36px; }
.knowledge-page > * { width: 100%; max-width: 1080px; margin-left: auto; margin-right: auto; }
.knowledge-header { display: flex; gap: 16px; align-items: center; margin-bottom: 32px; }
.knowledge-header h1 { font-size: 26px; font-weight: 600; letter-spacing: -.7px; margin: 0; }
.subtitle { font-size: 13px; color: var(--muted); margin: 9px 0 0; }
.btn-back { display: grid; place-items: center; width: 34px; height: 34px; border: 1px solid var(--line); border-radius: 8px; }
.btn-back:hover { background: var(--surface); }
.knowledge-cols { display: grid; grid-template-columns: minmax(260px, .8fr) minmax(320px, 1.2fr); gap: 24px; }
.knowledge-left, .knowledge-right { min-width: 0; }
.card { border: 1px solid var(--line); border-radius: 12px; padding: 22px; margin-bottom: 20px; background: white; }
.card h2 { font-size: 15px; font-weight: 600; margin: 0; }
.card-header { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; justify-content: space-between; margin-bottom: 18px; }
.hint { font-size: 12px; line-height: 1.7; color: var(--muted); margin: 12px 0 16px; }
.dropzone { border: 1px dashed #cdcdcd; border-radius: 9px; background: #fafafa; min-height: 150px; padding: 28px 16px; display: grid; place-items: center; text-align: center; font-size: 13px; color: #666; cursor: pointer; }
.dropzone::before { content: '↑'; font-size: 28px; color: #555; }
.dropzone p { margin: 10px 0 0; }
.dropzone.over, .dropzone:hover { border-color: #777; background: #f1f1f1; }
.dropzone.busy { opacity: .5; cursor: wait; }
.health-actions, .file-header-actions, .file-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.btn-ghost, .btn-danger { display: inline-flex; align-items: center; gap: 6px; border: 1px solid var(--line); border-radius: 6px; padding: 6px 9px; font-size: 11px; white-space: nowrap; }
.btn-ghost:hover:not(:disabled) { background: var(--surface); border-color: #ccc; }
.btn-danger { color: var(--danger); border-color: transparent; }
.btn-danger:hover:not(:disabled) { background: #fff1ef; }
.btn-spinner { width: 11px; height: 11px; border: 1px solid #ccc; border-top-color: #555; border-radius: 50%; animation: spin .8s linear infinite; }
.file-list { list-style: none; padding: 0; margin: 0; }
.file-list li { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 14px 0; border-bottom: 1px solid var(--line); }
.file-list li:last-child { border-bottom: 0; }
.file-name { font-size: 13px; overflow-wrap: anywhere; }
.file-actions { flex-shrink: 0; }
.file-status { font-size: 10px; padding: 4px 7px; border-radius: 5px; }
.file-status.indexed { color: var(--success); background: #edf8f1; }
.file-status.pending { color: #986918; background: #fff7e5; }
.card > .empty { min-height: 180px; font-size: 13px; color: var(--muted); }
.health-summary { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; font-size: 13px; }
.health-time { width: 100%; color: var(--muted); font-size: 11px; margin-top: 5px; }
.health-dot { width: 7px; height: 7px; border-radius: 50%; }
.health-dot.ok { background: var(--success); }
.health-dot.bad { background: var(--danger); }
.health-issues { list-style: none; margin: 0; padding: 0; }
.health-issues li { display: flex; align-items: baseline; gap: 8px; border-top: 1px solid var(--line); padding: 12px 0; font-size: 12px; }
.issue-sev { font-size: 10px; padding: 2px 5px; background: #fff7e5; color: #986918; flex-shrink: 0; border-radius: 4px; }
.issue-sev.error { background: #fff1ef; color: var(--danger); }
.issue-detail { display: block; font-style: normal; color: var(--muted); line-height: 1.7; margin-top: 5px; overflow-wrap: anywhere; }
.toast { padding: 12px 16px; margin-bottom: 20px; border-radius: 8px; background: #edf8f1; color: var(--success); font-size: 13px; }
.toast.error { background: #fff1ef; color: var(--danger); }
@media (max-width: 1100px) {
  .knowledge-cols { grid-template-columns: 1fr; }
  .knowledge-left { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
}
@media (max-width: 760px) {
  .knowledge-page { padding: 40px 16px 24px; }
  .knowledge-left { display: block; }
  .knowledge-cols { display: block; }
  .card { padding: 18px; }
  .file-list li { align-items: flex-start; flex-direction: column; }
}
</style>
