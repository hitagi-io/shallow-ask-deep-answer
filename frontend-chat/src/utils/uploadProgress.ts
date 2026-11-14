import { isRecord, isCount } from './validation'

export const indexStages = ['preparing', 'parsing', 'splitting', 'embedding', 'writing', 'verifying', 'finalizing'] as const
export type IndexStage = typeof indexStages[number]
export interface IndexProgress {
  stage: IndexStage
  completed: number
  total: number
  percent: number
}

// 与服务端约定一致：传输和七个入库阶段等权，不用等待时间推测进度。
export const fileProgressStageCount = indexStages.length + 1
export const transferredPercent = Math.floor(100 / fileProgressStageCount)

export interface UploadTask {
  task_id: string
  status: 'queued' | 'running' | 'completed' | 'failed'
  total: number
  completed: number
  succeeded: number
  failed: number
  error: string | null
  files: {
    name: string
    status: 'queued' | 'running' | 'success' | 'error'
    overwritten: boolean | null
    indexed: boolean | null
    error: string | null
    progress: IndexProgress | null
    /** 已成功入库的文件当前是否仍在；其他任务状态不检查，值为 null。 */
    file_exists: boolean | null
  }[]
}

const stageLabels: Record<IndexStage, string> = {
  preparing: '正在检查文件', parsing: '正在解析', splitting: '正在分块',
  embedding: '正在向量化', writing: '正在写入索引', verifying: '正在校验', finalizing: '正在完成入库',
}

export function taskFileDetail(file: UploadTask['files'][number]): string {
  if (file.error) return file.error
  if (file.status === 'success' && file.file_exists === false) return '文件已删除'
  if (file.status === 'running' && file.progress !== null) {
    const { stage, completed, total } = file.progress
    return `${stageLabels[stage]}（${completed}/${total}）`
  }
  return { queued: '排队等待入库', running: '正在解析和索引', success: file.overwritten ? '已覆盖并入库' : '已入库', error: '入库失败，请重新上传' }[file.status]
}

export function taskFilePercent(file: UploadTask['files'][number]): number {
  if (file.status === 'success') return 100
  // 队列中或升级前已开始的任务没有阶段记录，只确认传输已完成。
  return file.progress === null ? transferredPercent : file.progress.percent
}

function isNullableText(value: unknown): value is string | null {
  return value === null || typeof value === 'string'
}
function isNullableBoolean(value: unknown): value is boolean | null {
  return value === null || typeof value === 'boolean'
}
function isStage(value: unknown): value is IndexStage {
  return indexStages.some(stage => stage === value)
}
function invalidProgress(): never {
  throw new Error('上传进度数据格式无效，请检查后端是否已更新')
}
function parseProgress(value: unknown): IndexProgress | null {
  if (value === null) return null
  if (!isRecord(value) || !isStage(value.stage) || !isCount(value.completed) || !isCount(value.total)
      || value.total === 0 || value.completed > value.total || !isCount(value.percent) || value.percent > 100) return invalidProgress()
  return { stage: value.stage, completed: value.completed, total: value.total, percent: value.percent }
}

export function parseUploadTasks(value: unknown): UploadTask[] {
  if (!isRecord(value) || !Array.isArray(value.tasks)) return invalidProgress()
  return value.tasks.map((task: unknown): UploadTask => {
    if (!isRecord(task) || typeof task.task_id !== 'string'
        || (task.status !== 'queued' && task.status !== 'running' && task.status !== 'completed' && task.status !== 'failed')
        || !isCount(task.total) || !isCount(task.completed) || !isCount(task.succeeded) || !isCount(task.failed)
        || !isNullableText(task.error) || !Array.isArray(task.files)) return invalidProgress()
    const files = task.files.map((file: unknown): UploadTask['files'][number] => {
      if (!isRecord(file) || typeof file.name !== 'string'
          || (file.status !== 'queued' && file.status !== 'running' && file.status !== 'success' && file.status !== 'error')
          || !isNullableBoolean(file.overwritten) || !isNullableBoolean(file.indexed) || !isNullableText(file.error)) return invalidProgress()
      if (!isNullableBoolean(file.file_exists)
          || (file.status === 'success' ? file.file_exists === null : file.file_exists !== null)) return invalidProgress()
      return { name: file.name, status: file.status, overwritten: file.overwritten, indexed: file.indexed,
        error: file.error, progress: parseProgress(file.progress), file_exists: file.file_exists }
    })
    return { task_id: task.task_id, status: task.status, total: task.total, completed: task.completed,
      succeeded: task.succeeded, failed: task.failed, error: task.error, files }
  })
}
