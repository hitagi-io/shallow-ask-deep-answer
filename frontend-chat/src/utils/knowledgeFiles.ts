import { isRecord, isCount } from './validation'

export type FileFilter = 'all' | 'indexed' | 'pending'
export type FileSort = 'uploaded_desc' | 'uploaded_asc' | 'name_asc' | 'name_desc' | 'pending_first' | 'indexed_first'
export const fileSortLabels: Record<FileSort, string> = {
  uploaded_desc: '上传时间：从新到旧',
  uploaded_asc: '上传时间：从旧到新',
  name_asc: '文件名：升序',
  name_desc: '文件名：降序',
  pending_first: '未入库优先',
  indexed_first: '已入库优先',
}
export interface FileItem {
  name: string
  path: string
  /** 只有当前有效且已发布的入库记录才为 true；完整性另见检查报告。 */
  indexed: boolean
}
export interface FileSummary { total: number; indexed: number }
export interface FilePage {
  files: FileItem[]
  page: number
  page_size: number
  total_pages: number
  matched_total: number
  summary: FileSummary
}
export const FILE_PAGE_SIZE = 10

export function parseFileSummary(value: unknown): FileSummary {
  if (!isRecord(value) || !isCount(value.total) || !isCount(value.indexed) || value.indexed > value.total) {
    throw new Error('项目资料统计数据格式无效')
  }
  return { total: value.total, indexed: value.indexed }
}
export function parseFilePage(value: unknown): FilePage {
  if (!isRecord(value) || !Array.isArray(value.files) || !isCount(value.matched_total)
      || !isCount(value.page) || value.page < 1 || value.page_size !== FILE_PAGE_SIZE || !isCount(value.total_pages)) {
    throw new Error('项目资料分页数据格式无效')
  }
  const summary = parseFileSummary(value.summary)
  const files = value.files.map((file: unknown): FileItem => {
    if (!isRecord(file) || typeof file.name !== 'string' || !file.name || typeof file.path !== 'string'
        || !file.path || typeof file.indexed !== 'boolean') throw new Error('项目资料数据格式无效')
    return { name: file.name, path: file.path, indexed: file.indexed }
  })
  const totalPages = Math.ceil(value.matched_total / value.page_size)
  const expectedFiles = Math.min(value.page_size, value.matched_total - (value.page - 1) * value.page_size)
  if (value.matched_total > summary.total || value.total_pages !== totalPages
      || value.page > Math.max(1, totalPages) || files.length !== expectedFiles) throw new Error('项目资料分页数量无效')
  return { files, page: value.page, page_size: value.page_size, total_pages: value.total_pages,
    matched_total: value.matched_total, summary }
}
