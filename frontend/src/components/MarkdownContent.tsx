import { Children, isValidElement, type ReactNode } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import MermaidDiagram from './MermaidDiagram'
import 'katex/dist/katex.min.css'
import styles from './MarkdownContent.module.css'

function CodeBlock({ children }: { children?: ReactNode }) {
  const child = Children.toArray(children)[0]
  if (isValidElement<{ className?: string; children?: ReactNode }>(child) && child.props.className === 'language-mermaid') {
    return <MermaidDiagram source={String(child.props.children ?? '').trim()} />
  }
  return <pre>{children}</pre>
}

export default function MarkdownContent({ children }: { children: string }) {
  return <div className={styles.markdown}>
    <ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[[rehypeKatex, { trust: false, strict: 'warn' }]]}
      skipHtml components={{
        pre: CodeBlock,
        table: ({ children }) => <div className={styles.tableScroll}><table>{children}</table></div>,
        a: ({ children, href }) => <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>,
      }}>{children}</ReactMarkdown>
  </div>
}
