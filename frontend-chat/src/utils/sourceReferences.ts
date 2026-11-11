import { isSourceUrl } from '../api'

/** 只折叠服务端追加的文末来源列表，正文中的引用仍可直接打开原文。 */
export function collapseSourceReferences(doc: Document, deleted: boolean) {
  const list = doc.body.lastElementChild
  const heading = list?.previousElementSibling
  if (list?.tagName !== 'UL' || heading?.tagName !== 'P'
    || heading.textContent !== '参考来源（点击查看原文）') return

  const groups = new Map<string, { name: string; links: { link: HTMLAnchorElement; label: string }[] }>()
  for (const item of list.children) {
    const link = item.querySelector<HTMLAnchorElement>('a[href]')
    const href = link?.getAttribute('href') || ''
    if (item.tagName !== 'LI' || !link || !isSourceUrl(href)) return
    // 按项目和文件路径归组，查询参数中的页段坐标不参与分组。
    const path = href.slice(0, href.indexOf('?'))
    let name: string
    try { name = decodeURIComponent(path.slice(path.lastIndexOf('/') + 1)) }
    catch { return }
    const text = link.textContent || ''
    const number = text.match(/^\[\d+\] /)?.[0]
    const prefix = `${number}${name} · `
    if (!number || !text.startsWith(prefix)) return
    const group = groups.get(path) || { name, links: [] }
    group.links.push({ link, label: number + text.slice(prefix.length) })
    groups.set(path, group)
  }
  if (!groups.size) return

  const references = doc.createElement('div')
  references.className = 'source-references'
  for (const { name, links } of groups.values()) {
    const details = doc.createElement('details')
    details.className = 'source-file'
    const summary = doc.createElement('summary')
    summary.title = '展开或收起具体引用'
    const filename = doc.createElement('span')
    filename.textContent = name + (deleted ? '（已删除）' : '')
    summary.append(filename)
    const passages = doc.createElement('ul')
    for (const { link, label } of links) {
      const item = doc.createElement('li')
      link.textContent = label
      item.append(link)
      passages.append(item)
    }
    details.append(summary, passages)
    references.append(details)
  }
  heading.textContent = deleted ? '参考来源（已删除，展开查看引用）' : '参考来源（展开文件查看具体引用）'
  heading.classList.add('source-references-heading')
  list.replaceWith(references)
}
