import { Fragment, useRef, useState, type ReactNode } from 'react'
import { copyToClipboard } from '../../components/clipboard'

// A markdown-LITE renderer, not a CommonMark implementation. Model output
// covers a predictable subset -- paragraphs, emphasis, inline code, fenced
// code, lists, blockquotes, links -- and that is all this parses. It builds
// React elements directly rather than an HTML string: there is no
// `dangerouslySetInnerHTML` anywhere below, which is what makes this safe
// against a model emitting hostile markup. A link (or a bare URL) only
// becomes a real `<a>` when its scheme is `http:`, `https:` or `mailto:`;
// anything else -- in particular `javascript:` -- simply fails to match and
// renders as the literal characters the model sent, the same way a malformed
// fence or an unclosed `**` falls back to plain text rather than erroring.

type Block =
  | { type: 'p'; text: string }
  | { type: 'heading'; level: number; text: string }
  | { type: 'code'; lang: string; code: string }
  | { type: 'ul' | 'ol'; items: string[] }
  | { type: 'quote'; text: string }

const FENCE = /^```(\S*)\s*$/
const HEADING = /^(#{1,6})\s+(.*)$/
const BULLET = /^(-|\*|\+)\s+/
const NUMBERED = /^\d+[.)]\s+/
const QUOTE = /^>\s?/

/** Line-oriented block parser. Each branch below is greedy: it consumes every
 *  consecutive line that still matches its own pattern before falling back to
 *  the loop, so `- a\n- b` is one list rather than two. A paragraph consumes
 *  everything that ISN'T the start of one of the other block types, which is
 *  what lets prose and lists interrupt each other without a blank line
 *  between them, the way model output actually reads. */
function parseBlocks(source: string): Block[] {
  const lines = source.replace(/\r\n/g, '\n').split('\n')
  // `noUncheckedIndexedAccess` types `lines[i]` as `string | undefined` no
  // matter how the loop bounds are checked -- `at` is the one place that gets
  // asserted back to `string`, always behind an `i < lines.length` check.
  const at = (idx: number): string => lines[idx] ?? ''
  const blocks: Block[] = []
  let i = 0

  while (i < lines.length) {
    const line = at(i)

    if (line.trim() === '') {
      i++
      continue
    }

    const fence = line.match(FENCE)
    if (fence) {
      const lang = fence[1] ?? ''
      const code: string[] = []
      i++
      while (i < lines.length && !/^```\s*$/.test(at(i))) {
        code.push(at(i))
        i++
      }
      // An unterminated fence (stream still arriving) runs to the end rather
      // than being dropped -- the partial code is still worth reading.
      if (i < lines.length) i++
      blocks.push({ type: 'code', lang, code: code.join('\n') })
      continue
    }

    const heading = line.match(HEADING)
    if (heading) {
      const level = (heading[1] ?? '').length
      blocks.push({ type: 'heading', level, text: (heading[2] ?? '').trim() })
      i++
      continue
    }

    if (BULLET.test(line)) {
      const items: string[] = []
      while (i < lines.length && BULLET.test(at(i))) {
        items.push(at(i).replace(BULLET, ''))
        i++
      }
      blocks.push({ type: 'ul', items })
      continue
    }

    if (NUMBERED.test(line)) {
      const items: string[] = []
      while (i < lines.length && NUMBERED.test(at(i))) {
        items.push(at(i).replace(NUMBERED, ''))
        i++
      }
      blocks.push({ type: 'ol', items })
      continue
    }

    if (QUOTE.test(line)) {
      const quote: string[] = []
      while (i < lines.length && QUOTE.test(at(i))) {
        quote.push(at(i).replace(QUOTE, ''))
        i++
      }
      blocks.push({ type: 'quote', text: quote.join('\n') })
      continue
    }

    const para: string[] = []
    while (
      i < lines.length &&
      at(i).trim() !== '' &&
      !FENCE.test(at(i)) &&
      !HEADING.test(at(i)) &&
      !BULLET.test(at(i)) &&
      !NUMBERED.test(at(i)) &&
      !QUOTE.test(at(i))
    ) {
      para.push(at(i))
      i++
    }
    blocks.push({ type: 'p', text: para.join('\n') })
  }

  return blocks
}

// One alternation, tried left to right at every position: inline code, bold
// (`**`/`__`), italic (`*`/`_`), a `[text](url)` link, then a bare URL. Built
// fresh per call rather than held at module scope -- a shared `RegExp` with
// `/g` carries `lastIndex` as mutable state, and nothing here needs that risk
// for the sake of skipping one allocation.
function inlinePattern(): RegExp {
  return /`([^`]+)`|\*\*([^*]+)\*\*|__([^_]+)__|\*([^*\s][^*]*)\*|_([^_\s][^_]*)_|\[([^\]]+)\]\((https?:\/\/[^\s)]+|mailto:[^\s)]+)\)|(https?:\/\/[^\s<>()]+)/g
}

function renderText(text: string, key: string): ReactNode[] {
  const out: ReactNode[] = []
  const lines = text.split('\n')
  lines.forEach((line, i) => {
    if (i > 0) out.push(<br key={`${key}-br${i}`} />)
    if (line) out.push(<Fragment key={`${key}-t${i}`}>{line}</Fragment>)
  })
  return out
}

function renderInline(text: string, key: string): ReactNode[] {
  const pattern = inlinePattern()
  const out: ReactNode[] = []
  let lastIndex = 0
  let n = 0
  let match: RegExpExecArray | null
  while ((match = pattern.exec(text))) {
    if (match.index > lastIndex) {
      out.push(...renderText(text.slice(lastIndex, match.index), `${key}-${n++}`))
    }
    const mkey = `${key}-${n++}`
    if (match[1] !== undefined) {
      out.push(<code key={mkey}>{match[1]}</code>)
    } else if (match[2] !== undefined || match[3] !== undefined) {
      out.push(<strong key={mkey}>{match[2] ?? match[3]}</strong>)
    } else if (match[4] !== undefined || match[5] !== undefined) {
      out.push(<em key={mkey}>{match[4] ?? match[5]}</em>)
    } else if (match[6] !== undefined && match[7] !== undefined) {
      out.push(
        <a key={mkey} href={match[7]} target="_blank" rel="noopener noreferrer">
          {match[6]}
        </a>,
      )
    } else if (match[8] !== undefined) {
      out.push(
        <a key={mkey} href={match[8]} target="_blank" rel="noopener noreferrer">
          {match[8]}
        </a>,
      )
    }
    lastIndex = pattern.lastIndex
  }
  if (lastIndex < text.length) out.push(...renderText(text.slice(lastIndex), `${key}-${n++}`))
  return out
}

const HEADING_SCALE = ['1.25em', '1.15em', '1.08em', '1.02em', '0.98em', '0.95em']

function CodeBlock({ lang, code }: { lang: string; code: string }) {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const timer = useRef<number | undefined>(undefined)

  const copy = async () => {
    const result = await copyToClipboard(code)
    setState(result)
    window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => setState('idle'), 2000)
  }

  return (
    <div className="codeblock">
      <div className="codeblockhead">
        <span className="unit mono">{lang || 'text'}</span>
        <button type="button" onClick={() => void copy()}>
          {state === 'copied' ? 'Copied' : state === 'failed' ? 'Select to copy' : 'Copy'}
        </button>
      </div>
      <pre>
        <code>{code}</code>
      </pre>
    </div>
  )
}

/** Renders one turn's text as markdown-lite. `live` mirrors the `aria-live`
 *  the plain-text version used to carry directly on its `<p>` -- moved here
 *  because a streaming reply can now be more than one block. */
export function MessageBody({ text, live }: { text: string; live?: boolean }) {
  const blocks = parseBlocks(text)
  return (
    <div className="mdbody" aria-live={live ? 'polite' : undefined}>
      {blocks.map((block, i) => {
        const key = `b${i}`
        switch (block.type) {
          case 'code':
            return <CodeBlock key={key} lang={block.lang} code={block.code} />
          case 'heading':
            return (
              <p
                key={key}
                style={{ fontWeight: 600, fontSize: HEADING_SCALE[block.level - 1] ?? '1em' }}
              >
                {renderInline(block.text, key)}
              </p>
            )
          case 'ul':
            return (
              <ul key={key}>
                {block.items.map((item, j) => (
                  <li key={`${key}-${j}`}>{renderInline(item, `${key}-${j}`)}</li>
                ))}
              </ul>
            )
          case 'ol':
            return (
              <ol key={key}>
                {block.items.map((item, j) => (
                  <li key={`${key}-${j}`}>{renderInline(item, `${key}-${j}`)}</li>
                ))}
              </ol>
            )
          case 'quote':
            return <blockquote key={key}>{renderInline(block.text, key)}</blockquote>
          case 'p':
          default:
            return <p key={key}>{renderInline(block.text, key)}</p>
        }
      })}
    </div>
  )
}
