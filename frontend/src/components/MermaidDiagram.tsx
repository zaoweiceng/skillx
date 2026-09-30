import { useEffect, useId, useState } from 'react'
import DOMPurify from 'dompurify'
import styles from './MarkdownContent.module.css'

export default function MermaidDiagram({ source }: { source: string }) {
  const id = `mermaid-${useId().replace(/[^a-zA-Z0-9-]/g, '')}`
  const [result, setResult] = useState({ source: '', svg: '', error: '' })
  useEffect(() => {
    let active = true
    async function render() {
      try {
        const { default: mermaid } = await import('mermaid')
        mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', theme: 'neutral',
          suppressErrorRendering: true, htmlLabels: false, flowchart: { htmlLabels: false } })
        const { svg } = await mermaid.render(id, source)
        const clean = DOMPurify.sanitize(svg, { USE_PROFILES: { svg: true, svgFilters: true } })
        if (active) setResult({ source, svg: clean, error: '' })
      } catch {
        if (active) setResult({ source, svg: '', error: '流程图语法有误，无法渲染。可展开源码查看。' })
      }
    }
    void render()
    return () => { active = false }
  }, [id, source])
  const current = result.source === source
  return <figure className={styles.diagram}>
    {current && result.svg ? <div role="img" aria-label="Mermaid 流程图" className={styles.diagramImage} dangerouslySetInnerHTML={{ __html: result.svg }} />
      : <div role="status">{current && result.error ? result.error : '正在渲染流程图…'}</div>}
    <details><summary>查看流程图源码</summary><pre><code>{source}</code></pre></details>
  </figure>
}
