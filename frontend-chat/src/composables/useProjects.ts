import { computed, onMounted, onUnmounted, ref } from 'vue'
import { deleteProject, fetchProjects, saveProject, type Project } from '../api'
import type { ConfirmAction } from './useConfirmation'

export function useProjects(confirmAction: ConfirmAction, initialSelectedId: string | null = null) {
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
      const selection = selectedId.value ?? initialSelectedId
      projects.value = result
      selectedId.value = result.some(project => project.id === selection) ? selection : null
      initialSelectedId = null
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
    if (!project || saving.value || !await confirmAction(`删除项目“${project.name}”及其所有文档和索引？历史对话将保留供查看，来源标记为“已删除”，无法继续使用该项目提问。文档和索引删除后无法恢复。`)) return false
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
