import { isSourceUrl } from '../api'

/** 内部证据标记不属于回答；流式未收齐的标记也不显示。 */
export function stripInternalCitations(text: string, streaming: boolean): string {
  const clean = text.replace(/\[\[cite:[^\]]*(?:\]\]?|$)/g, '')
  const prefix = '[[cite:'
  // 停止生成后也隐藏半截内部标记；完整答案末尾的普通单个方括号仍保留。
  for (let length = prefix.length - 1; length >= (streaming ? 1 : 2); length--) {
    if (clean.endsWith(prefix.slice(0, length))) return clean.slice(0, -length)
  }
  return clean
}

/** 文末只显示来源名称；同一文件的各处证据在原文窗口内切换。 */
export function formatSourceReferences(doc: Document, deleted: boolean) {
  const list = doc.body.lastElementChild
  const heading = list?.previousElementSibling
  if (list?.tagName !== 'UL' || heading?.tagName !== 'P'
    || !['来源：', '来源', '参考来源（点击查看原文）'].includes(heading.textContent || '')) return

  const groups = new Map<string, { name: string; urls: string[]; knowledge: boolean }>()
  const citedUrls = new Set<string>()
  for (const item of list.children) {
    const link = item.querySelector<HTMLAnchorElement>('a[href]')
    const href = link?.getAttribute('href') || ''
    if (item.tagName !== 'LI' || !link || item.textContent?.trim() !== link.textContent?.trim()) return
    const knowledge = isSourceUrl(href)
    if (!knowledge && !/^https?:\/\//i.test(href)) return
    const key = knowledge ? href.slice(0, href.indexOf('?')) : href
    let name = link.textContent || ''
    if (knowledge) {
      try { name = decodeURIComponent(key.slice(key.lastIndexOf('/') + 1)) }
      catch { return }
    }
    const group = groups.get(key) || { name, urls: [], knowledge }
    if (!group.urls.includes(href)) group.urls.push(href)
    groups.set(key, group)
    citedUrls.add(href)
  }
  if (!groups.size) return

  // 已保存的旧回答仍可阅读：只移除对应文末来源的数字链接，保留正文中的普通数字。
  for (const link of doc.querySelectorAll<HTMLAnchorElement>('a[href]')) {
    if (!list.contains(link) && citedUrls.has(link.getAttribute('href') || '')
      && /^\[?\d+\]?$/.test(link.textContent || '')) link.remove()
  }

  const references = doc.createElement('p')
  references.className = 'source-references'
  const label = doc.createElement('span')
  label.textContent = '来源：'
  references.append(label)
  for (const { name, urls, knowledge } of groups.values()) {
    const link = doc.createElement('a')
    link.setAttribute('href', urls[0])
    link.textContent = name
    if (knowledge) {
      // 原始 HTML 的 data 属性在调用本函数前已清洗；这里只写入已验证的本站来源。
      link.dataset.sourceUrls = JSON.stringify(urls)
      if (deleted) {
        link.append('（已删除）')
        link.classList.add('source-deleted')
        link.title = '来源已删除，点击查看说明'
      }
    } else {
      link.target = '_blank'
      link.rel = 'noopener noreferrer'
    }
    references.append(' ', link)
  }
  heading.remove()
  list.replaceWith(references)
}
