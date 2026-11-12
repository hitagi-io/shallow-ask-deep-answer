export const knowledgeFileAccept = '.txt,.md,.pdf,.docx,.xlsx,.pptx,.png,.jpg,.jpeg,.webp,.bmp,.tif,.tiff'
export const knowledgeFileTypes = 'TXT、Markdown、PDF、DOCX、XLSX、PPTX、PNG、JPG/JPEG、WebP、BMP、TIF/TIFF'
const extensions = new Set(knowledgeFileAccept.split(','))
export function supportsKnowledgeFile(name: string): boolean {
  return extensions.has(name.slice(name.lastIndexOf('.')).toLowerCase())
}
