/** 将耗时秒数格式化为分钟和秒。 */
export function formatDuration(seconds: number): string {
  const m = Math.floor(seconds / 60)
  const s = (seconds % 60).toFixed(1)
  return m > 0 ? `${m}分${s}秒` : `${s}秒`
}
