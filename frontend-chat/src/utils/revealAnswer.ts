/** 按已清洗的 Markdown 结构依次插入文字，保留链接、表格和代码块。 */
function* appendTextNodes(source: Node, target: Node): Generator<{ node: Text; characters: string[] }> {
  for (const child of source.childNodes) {
    if (child.nodeType === Node.TEXT_NODE) {
      const node = target.appendChild(document.createTextNode(''))
      yield { node, characters: Array.from((child as Text).data) }
    } else {
      const element = target.appendChild(child.cloneNode(false))
      yield* appendTextNodes(child, element)
    }
  }
}

export function revealAnswer(target: HTMLElement, sanitizedHtml: string, progress: () => void, finish: () => void) {
  const template = document.createElement('template')
  template.innerHTML = sanitizedHtml
  // 来源与操作随最终全文一次显示，链接地址始终保留在属性中。
  const footer = template.content.lastElementChild
  if (footer?.matches('p.source-references')) footer.remove()
  target.replaceChildren()
  const parts = appendTextNodes(template.content, target)
  const length = template.content.textContent?.length ?? 0
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches
  const interval = reducedMotion ? 160 : 32
  const charactersPerMs = reducedMotion ? length / 640 : Math.max(0.05, length / 3000)
  const firstBatch = reducedMotion ? Math.ceil(length / 4) : 1
  const startedAt = performance.now()
  let part = parts.next()
  let displayed = 0
  let offset = 0
  let timer: number | undefined

  function step() {
    if (document.hidden || !length) { finish(); return }
    const limit = firstBatch + Math.floor((performance.now() - startedAt) * charactersPerMs)
    while (!part.done && displayed < limit) {
      const { node, characters } = part.value
      const end = Math.min(characters.length, offset + limit - displayed)
      node.appendData(characters.slice(offset, end).join(''))
      displayed += end - offset
      offset = end
      if (offset === characters.length) {
        part = parts.next()
        offset = 0
      }
    }
    if (part.done) { finish(); return }
    progress()
    timer = window.setTimeout(step, interval)
  }
  step()
  return () => window.clearTimeout(timer)
}
