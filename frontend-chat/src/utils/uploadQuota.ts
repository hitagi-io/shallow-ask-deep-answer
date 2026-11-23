import { isCount, isRecord } from './validation'

interface UploadQuotaAmounts {
  storage_bytes: number
  daily_bytes: number
  pending_tasks: number
  embedding_chars: number
}

export interface UploadQuota {
  usage: UploadQuotaAmounts & { reserved_bytes: number; resets_at: string }
  limits: UploadQuotaAmounts & { file_bytes: number; request_bytes: number }
}

function validAmounts(value: unknown, minimum: number): value is UploadQuotaAmounts & Record<string, unknown> {
  return isRecord(value) && ['storage_bytes', 'daily_bytes', 'pending_tasks', 'embedding_chars'].every(key => {
    const amount = value[key]
    return isCount(amount) && amount >= minimum
  })
}

export function parseUploadQuota(value: unknown): UploadQuota {
  if (isRecord(value)) {
    const { usage, limits } = value
    if (validAmounts(usage, 0) && isCount(usage.reserved_bytes) && typeof usage.resets_at === 'string'
        && Number.isFinite(Date.parse(usage.resets_at)) && validAmounts(limits, 1)
        && isCount(limits.file_bytes) && limits.file_bytes > 0 && isCount(limits.request_bytes) && limits.request_bytes > 0) {
      return { usage: { ...usage, reserved_bytes: usage.reserved_bytes, resets_at: usage.resets_at },
        limits: { ...limits, file_bytes: limits.file_bytes, request_bytes: limits.request_bytes } }
    }
  }
  throw new Error('上传额度数据格式无效')
}

const decimal = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 1 })
const byteUnits = ['B', 'KiB', 'MiB', 'GiB', 'TiB']

export function formatQuotaBytes(bytes: number): string {
  let unit = 0
  let amount = bytes
  while (amount >= 1024 && unit < byteUnits.length - 1) {
    amount /= 1024
    unit++
  }
  return `${decimal.format(amount)} ${byteUnits[unit]}`
}
