import { Fragment, type ReactNode } from 'react'

// A model's answer, rendered as the markdown it was written in -- the subset
// chat models actually emit, and nothing that could execute.
//
// Hand-rolled rather than a dependency on purpose: everything is a React
// element, so a model that writes `<img onerror=…>` or a `javascript:` link
// gets literal text, and there is no `dangerouslySetInnerHTML` anywhere for a
// sanitizer to be forgotten in front of. Links open only for http(s). Images
// and raw HTML stay text. A fence that has not closed yet runs to the end, so
// a code block that is still streaming renders as code.

export type Block =
  | { kind: 'fence'; lang: string; text: string }
  | { kind: 'heading'; text: string }
  | { kind: 'hr' }
  | { kind: 'quote'; text: string }
  | { kind: 'ul'; items: string[] }
  | { kind: 'ol'; start: number; items: string[] }
  | { kind: 'p'; text: string }

const FENCE = /^ {0,3}(```|~~~)\s*([\w+-]*)\s*$/
const HEADING = /^ {0,3}#{1,6}\s+(.*?)\s*#*\s*$/
const HR = /^ {0,3}(-{3,}|\*{3,}|_{3,})\s*$/
const QUOTE = /^ {0,3}> ?(.*)$/
const UL = /^ {0,3}[-*+]\s+(.*)$/
const OL = /^ {0,3}(\d+)[.)]\s+(.*)$/
const CONTINUATION = /^ {2,}\S/
/** Every line that begins a block of its own and so ends a paragraph. */
const STARTS = [FENCE, HEADING, HR, QUOTE, UL, OL]

export function parseMarkdown(text: string): Block[] {
  const lines = text.replace(/\r\n/g, '\n').split('\n')
  // Every read below is guarded by `i < lines.length`; this only satisfies
  // the index type.
  const at = (n: number): string => lines[n] ?? ''
  const blocks: Block[] = []
  let i = 0
  while (i < lines.length) {
    const line = at(i)
    if (line.trim() === '') {
      i++
      continue
    }
    const fence = FENCE.exec(line)
    if (fence) {
      const marker = fence[1] ?? '```'
      const body: string[] = []
      i++
      while (i < lines.length && !at(i).trimStart().startsWith(marker)) body.push(at(i++))
      i++ // the closing fence, or past the end when it has not arrived yet
      blocks.push({ kind: 'fence', lang: fence[2] ?? '', text: body.join('\n') })
      continue
    }
    const heading = HEADING.exec(line)
    if (heading) {
      blocks.push({ kind: 'heading', text: heading[1] ?? '' })
      i++
      continue
    }
    if (HR.test(line)) {
      blocks.push({ kind: 'hr' })
      i++
      continue
    }
    if (QUOTE.test(line)) {
      const body: string[] = []
      while (i < lines.length) {
        const m = QUOTE.exec(at(i))
        if (!m) break
        body.push(m[1] ?? '')
        i++
      }
      blocks.push({ kind: 'quote', text: body.join('\n') })
      continue
    }
    const ordered = OL.exec(line)
    if (UL.test(line) || ordered) {
      const re = ordered ? OL : UL
      const items: string[] = []
      while (i < lines.length) {
        const m = re.exec(at(i))
        if (m) {
          items.push((ordered ? m[2] : m[1]) ?? '')
          i++
        } else if (items.length > 0 && CONTINUATION.test(at(i))) {
          items[items.length - 1] += `\n${at(i).trim()}`
          i++
        } else break
      }
      blocks.push(ordered ? { kind: 'ol', start: Number(ordered[1]), items } : { kind: 'ul', items })
      continue
    }
    // The first line cannot start a block (every branch above declined it),
    // so this always consumes at least one line.
    const body: string[] = []
    while (i < lines.length && at(i).trim() !== '' && !STARTS.some((re) => re.test(at(i)))) {
      body.push(at(i++))
    }
    blocks.push({ kind: 'p', text: body.join('\n') })
  }
  return blocks
}

// ── Inline ──────────────────────────────────────────────────────────────────

/** Code first, so nothing inside backticks is read as emphasis or a link. An
 *  image (`![alt](src)`) is not a link: the `!` lookbehind leaves it as text. */
const INLINE = /(`+)([\s\S]*?[^`])\1(?!`)|\*\*(?=\S)([\s\S]*?\S)\*\*|(?<![\w*])\*(?=\S)([^*]*?\S)\*(?![\w*])|(?<![\w_])_(?=\S)([^_]*?\S)_(?![\w_])|(?<!!)\[([^\]\n]+)\]\(([^)\s]+)\)/g

const SAFE_URL = /^https?:\/\//i

function inline(text: string): ReactNode[] {
  const out: ReactNode[] = []
  let last = 0
  let k = 0
  for (const m of text.matchAll(INLINE)) {
    const at = m.index ?? 0
    if (at > last) out.push(text.slice(last, at))
    last = at + m[0].length
    if (m[2] !== undefined) out.push(<code key={k++}>{m[2]}</code>)
    else if (m[3] !== undefined) out.push(<strong key={k++}>{inline(m[3])}</strong>)
    else if (m[4] !== undefined) out.push(<em key={k++}>{inline(m[4])}</em>)
    else if (m[5] !== undefined) out.push(<em key={k++}>{inline(m[5])}</em>)
    else if (m[6] !== undefined && m[7] !== undefined && SAFE_URL.test(m[7])) {
      out.push(
        <a key={k++} href={m[7]} target="_blank" rel="noopener noreferrer">
          {inline(m[6])}
        </a>,
      )
    } else out.push(m[0])
  }
  if (last < text.length) out.push(text.slice(last))
  return out
}

function renderBlock(b: Block, key: number): ReactNode {
  switch (b.kind) {
    case 'fence':
      return (
        <pre key={key} data-lang={b.lang || undefined}>
          <code>{b.text}</code>
        </pre>
      )
    case 'heading':
      return (
        <p key={key} className="label">
          {inline(b.text)}
        </p>
      )
    case 'hr':
      return <hr key={key} />
    case 'quote':
      return <blockquote key={key}>{inline(b.text)}</blockquote>
    case 'ul':
      return (
        <ul key={key}>
          {b.items.map((it, j) => (
            <li key={j}>{inline(it)}</li>
          ))}
        </ul>
      )
    case 'ol':
      return (
        <ol key={key} start={b.start === 1 ? undefined : b.start}>
          {b.items.map((it, j) => (
            <li key={j}>{inline(it)}</li>
          ))}
        </ol>
      )
    case 'p':
      return <p key={key}>{inline(b.text)}</p>
  }
}

export function Markdown({ text }: { text: string }) {
  return <Fragment>{parseMarkdown(text).map(renderBlock)}</Fragment>
}
