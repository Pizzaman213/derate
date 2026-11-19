/** Splits a model's inline `<think>…</think>` block out of its answer text.
 *
 *  A deployment launched without `--reasoning-parser` leaves the reasoning in
 *  `delta.content`. It arrives in one of two shapes, and which one depends on
 *  the checkpoint's chat template, not on the family:
 *
 *  - **explicit**: the model writes `<think>` itself, then `</think>`.
 *  - **implicit**: the template already put `<think>\n` at the end of the
 *    prompt (Qwen3 Thinking/3.5+, DeepSeek-R1-0528, QwQ), so the output starts
 *    mid-thought and only `</think>` ever appears.
 *
 *  `startInside` says the caller expects the implicit shape: text streams as
 *  reasoning from the first delta (an explicit `<think>` at the head is still
 *  stripped). If the stream ends without `</think>`, the guess was wrong and
 *  `flush()` hands the whole text back as the answer, as a rewrite.
 *
 *  Without `startInside`, text streams as the answer; a `</think>` that turns
 *  up with no `<think>` before it means the template opened the block, and
 *  the splitter rewrites: everything before it was reasoning.
 *
 *  Tags can be split across any number of deltas, so a possible partial tag
 *  at the tail of a delta is held until the next one decides it. */

export interface ThinkSplit {
  reasoning: string
  content: string
  /** `reasoning` and `content` are the WHOLE of each channel so far,
   *  replacing what was emitted before, not increments. */
  rewrite?: true
}

export interface ThinkSplitter {
  /** Route one content delta. Either half may be ''. */
  push(text: string): ThinkSplit
  /** End of stream: whatever is still buffered, or a rewrite. */
  flush(): ThinkSplit
  /** True while the stream is understood to carry inline reasoning. */
  readonly inline: boolean
}

export interface ThinkSplitterOptions {
  open?: string
  close?: string
  /** The prompt already opened the block: start inside it. */
  startInside?: boolean
}

type State = 'undecided' | 'reasoning' | 'content'

const EMPTY: ThinkSplit = { reasoning: '', content: '' }

/** Length of the longest suffix of `text` that is a proper prefix of `tag`:
 *  the part that might still turn out to be the tag once the next delta lands. */
function partialTagSuffix(text: string, tag: string): number {
  for (let n = Math.min(text.length, tag.length - 1); n > 0; n--) {
    if (tag.startsWith(text.slice(text.length - n))) return n
  }
  return 0
}

export function createThinkSplitter({
  open = '<think>',
  close = '</think>',
  startInside = false,
}: ThinkSplitterOptions = {}): ThinkSplitter {
  let state: State = startInside ? 'reasoning' : 'undecided'
  /** In `reasoning`: entered without an opening tag (`startInside`). */
  let implicit = startInside
  let inline = startInside
  /** A closing tag has been consumed; nothing after it is reasoning. */
  let closed = false
  /** An opening tag appeared inside the answer, so a later `</think>` is
   *  text the answer is quoting, not a block boundary. */
  let quoted = false
  let buffer = ''
  /** Every pushed delta, verbatim: the answer, if `startInside` was wrong. */
  let raw = ''
  /** Answer text emitted so far, before any close: what a rewrite moves. */
  let emittedContent = ''
  /** Nothing has been emitted as reasoning yet: the template's newline after
   *  the opening tag is layout, not thought, and is dropped. */
  let fresh = true
  /** After a close: answer text has been emitted. Until it has, leading
   *  newlines are still the block's separator. */
  let answerStarted = true

  const afterClose = (rest: string): string => {
    const content = rest.replace(/^\n+/, '')
    answerStarted = content !== ''
    return content
  }

  /** In `reasoning`: emit what is certainly reasoning, close the block if the
   *  closing tag is complete, and keep a possible partial tag buffered. */
  const drainReasoning = (): ThinkSplit => {
    if (implicit && fresh) {
      // An explicit opening tag at the head of an implicit block is still the
      // model's own `<think>`; hold while the head could still become one.
      const head = buffer.trimStart()
      if (head.startsWith(open)) {
        buffer = head.slice(open.length)
        implicit = false
      } else if (open.startsWith(head)) {
        return EMPTY
      }
    }
    const at = buffer.indexOf(close)
    let reasoning: string
    let content = ''
    if (at !== -1) {
      reasoning = buffer.slice(0, at)
      content = afterClose(buffer.slice(at + close.length))
      buffer = ''
      state = 'content'
      closed = true
      implicit = false
    } else {
      const keep = partialTagSuffix(buffer, close)
      reasoning = buffer.slice(0, buffer.length - keep)
      buffer = buffer.slice(buffer.length - keep)
    }
    if (fresh) reasoning = reasoning.replace(/^\n+/, '')
    if (reasoning !== '') fresh = false
    return { reasoning, content }
  }

  /** In `content` before any close: watch for a bare `</think>`. */
  const drainContent = (): ThinkSplit => {
    const at = buffer.indexOf(close)
    if (at !== -1) {
      const before = emittedContent + buffer.slice(0, at)
      if (!before.includes(open)) {
        // The template opened the block; everything so far was reasoning.
        const content = afterClose(buffer.slice(at + close.length))
        buffer = ''
        closed = true
        inline = true
        fresh = false
        emittedContent = content
        return { rewrite: true, reasoning: before.replace(/^\n+/, '').replace(/\n+$/, ''), content }
      }
      quoted = true
    }
    if (!quoted && (emittedContent.slice(-open.length) + buffer).includes(open)) quoted = true
    const keep = quoted ? 0 : partialTagSuffix(buffer, close)
    const content = buffer.slice(0, buffer.length - keep)
    buffer = buffer.slice(buffer.length - keep)
    emittedContent += content
    return { reasoning: '', content }
  }

  return {
    get inline() {
      return inline
    },
    push(text: string): ThinkSplit {
      if (text === '') return EMPTY
      raw += text
      if (state === 'content') {
        if (closed) {
          if (answerStarted) return { reasoning: '', content: text }
          return { reasoning: '', content: afterClose(text) }
        }
        if (quoted) return { reasoning: '', content: text }
        buffer += text
        return drainContent()
      }
      buffer += text
      if (state === 'reasoning') return drainReasoning()
      const head = buffer.trimStart()
      if (head.startsWith(open)) {
        state = 'reasoning'
        inline = true
        buffer = head.slice(open.length)
        return drainReasoning()
      }
      // Whitespace only, or still a prefix of the opening tag: undecided.
      if (open.startsWith(head)) return EMPTY
      state = 'content'
      return drainContent()
    },
    flush(): ThinkSplit {
      const rest = buffer
      buffer = ''
      if (state === 'reasoning' && implicit) {
        // `startInside` guessed wrong: no block ever closed, so none was
        // open. All of it was the answer -- if there was any.
        inline = false
        if (raw === '') return EMPTY
        return { rewrite: true, reasoning: '', content: raw }
      }
      if (state === 'reasoning') return { reasoning: rest, content: '' }
      return { reasoning: '', content: rest }
    },
  }
}
