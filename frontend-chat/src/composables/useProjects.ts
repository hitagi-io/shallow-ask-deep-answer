import { computed, onMounted, onUnmounted, ref } from 'vue'
import { deleteProject, fetchProjects, saveProject, type Project } from '../api'
import type { ConfirmAction } from './useConfirmation'

export function useProjects(confirmAction: ConfirmAction) {
  const projects = ref<Project[]>([])
  const selectedId = ref<string | null>(null)
  const selected = computed(() => projects.value.find(project => project.id === selectedId.value) || null)
  const loading = ref(false)
  const saving = ref(false)
  const error = ref('')
  let disposed = false
  const errorText = (value: unknown) => value instanceof Error ? value.message : '项目操作失败，请重试'

  async function load() {
    if (loading.value || saving.value || disposed) return
    loading.value = true
    error.value = ''
    try {
      const result = await fetchProjects()
      if (disposed) return
      projects.value = result
      if (!result.some(project => project.id === selectedId.value)) selectedId.value = null
    } catch (value) { if (!disposed) error.value = errorText(value) }
    finally { loading.value = false }
  }

  async function save(name: string, id?: string) {
    if (saving.value || loading.value || !name.trim()) return false
    saving.value = true
    error.value = ''
    try {
      const project = await saveProject(name.trim(), id)
      if (disposed) return false
      const index = projects.value.findIndex(item => item.id === project.id)
      if (index < 0) projects.value.unshift(project)
      else projects.value[index] = project
      selectedId.value = project.id
      return true
    } catch (value) { if (!disposed) error.value = errorText(value); return false }
    finally { saving.value = false }
  }

  async function remove(id: string) {
    const project = projects.value.find(item => item.id === id)
    if (!project || saving.value || !await confirmAction(`删除项目“${project.name}”、其中所有文档和索引，以及该项目的相关对话？此操作无法撤销。`)) return false
    if (saving.value) return
    saving.value = true
    error.value = ''
    try {
      await deleteProject(id)
      if (disposed) return
      projects.value = projects.value.filter(item => item.id !== id)
      if (selectedId.value === id) selectedId.value = null
      return true
    } catch (value) { if (!disposed) error.value = errorText(value); return false }
    finally { saving.value = false }
  }

  onMounted(() => { void load() })
  onUnmounted(() => { disposed = true })
  return { projects, selectedId, selected, loading, saving, error, load, save, remove }
}
