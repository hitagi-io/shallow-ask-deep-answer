export async function copyText(text: string) {
  if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text)
  const active = document.activeElement as HTMLElement | null
  const area = document.createElement('textarea')
  area.value = text
  area.style.cssText = 'position:fixed;opacity:0;pointer-events:none'
  document.body.append(area)
  area.select()
  try { if (!document.execCommand('copy')) throw new Error('复制失败，请手动选择文本复制') }
  finally { area.remove(); active?.focus() }
}
