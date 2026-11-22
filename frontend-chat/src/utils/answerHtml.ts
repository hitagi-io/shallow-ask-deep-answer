import DOMPurify from 'dompurify'
import { marked } from 'marked'

/** 图片来源只在惰性模板内检查，插入页面之前将站外图片转换为普通链接。 */
export function answerHtml(text: string): string {
  const template = document.createElement('template')
  template.innerHTML = marked.parse(text, { breaks: true, gfm: true }) as string
  const content = template.content.ownerDocument.createElement('div')
  content.append(template.content)
  DOMPurify.sanitize(content, {
    IN_PLACE: true,
    ALLOWED_TAGS: ['p', 'div', 'span', 'br', 'hr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'a', 'img',
      'pre', 'code', 'kbd', 'blockquote', 'ol', 'ul', 'li', 'table', 'thead', 'tbody', 'tfoot', 'tr',
      'th', 'td', 'strong', 'b', 'em', 'i', 'del', 's', 'sub', 'sup', 'mark', 'details', 'summary', 'input'],
    ALLOWED_ATTR: ['href', 'src', 'alt', 'title', 'class', 'start', 'open', 'colspan', 'rowspan', 'align',
      'type', 'checked', 'disabled'],
    ALLOW_DATA_ATTR: false,
  })
  for (const input of content.querySelectorAll('input')) {
    if (input.type !== 'checkbox') input.remove()
    else { input.removeAttribute('src'); input.disabled = true }
  }
  for (const img of content.querySelectorAll('img')) {
    const src = img.getAttribute('src')
    const label = img.alt || '查看外部图片'
    if (src && /^data:image\/(?:png|jpeg|gif|webp|avif);base64,/i.test(src)) continue
    let url: URL | undefined
    try { if (src) url = new URL(src, document.baseURI) } catch { /* 只保留替代文字 */ }
    if (!url || !['https:', 'http:'].includes(url.protocol)) {
      img.replaceWith(img.alt)
    } else if (url.origin !== location.origin) {
      // 已有超链接时保留其原本的点击目标，避免嵌套链接。
      if (img.closest('a')) { img.replaceWith(label); continue }
      const link = content.ownerDocument.createElement('a')
      link.href = url.href
      link.textContent = label
      link.target = '_blank'
      link.rel = 'noopener noreferrer'
      img.replaceWith(link)
    }
  }
  return content.innerHTML
}
