// The inline <think> splitter, over the delta sequences a deployment without
// --reasoning-parser actually sends: tags cut across deltas at any byte, the
// template's newline before and after, a stream that ends mid-thought, a
// template that opened the block in the prompt so only `</think>` arrives,
// and the text that merely looks like a tag. A wrong split here either hides
// part of the answer inside a collapsed "Thinking" block or prints the
// thinking as the answer, and neither is visible to types.
import { load, report } from '../check/harness.mjs'

const M = await load(import.meta.url, './think.ts')
const { check, done } = report()

/** Feed every delta, flush, and return the two channels as a caller that
 *  honours rewrites would hold them. `steps` records each push's output. */
function run(deltas, opts) {
  const s = M.createThinkSplitter(opts)
  let reasoning = ''
  let content = ''
  let rewrites = 0
  const steps = []
  const apply = (p) => {
    steps.push(p)
    if (p.rewrite) {
      rewrites += 1
      reasoning = p.reasoning
      content = p.content
    } else {
      reasoning += p.reasoning
      content += p.content
    }
  }
  for (const d of deltas) apply(s.push(d))
  apply(s.flush())
  return { reasoning, content, inline: s.inline, rewrites, steps }
}

{
  const r = run(['<th', 'ink>\nplan ', 'ning</thi', 'nk>\n\nAnswer'])
  check(r.reasoning === 'plan ning', `tags split across deltas: reasoning ${JSON.stringify(r.reasoning)}`)
  check(r.content === 'Answer', `tags split across deltas: content ${JSON.stringify(r.content)}`)
  check(r.inline === true, 'an opening tag at the head marks the stream inline')
}
{
  const r = run(['\n\n<think>', 'x', '</think>', '\n', '\nY'])
  check(r.reasoning === 'x' && r.content === 'Y', `leading newline before <think> still detected; separator newlines in later deltas dropped (${JSON.stringify(r)})`)
}
{
  const r = run(['Hello <think>x</think>'])
  check(r.reasoning === '' && r.content === 'Hello <think>x</think>' && !r.inline, 'a tag after real text has started is content')
}
{
  const r = run(['Hello ', '<think>x', '</think>'])
  check(r.reasoning === '' && r.content === 'Hello <think>x</think>', 'a tag in a later delta after text is content')
}
{
  const s = M.createThinkSplitter()
  const a = s.push('<think>still ')
  const b = s.push('going</')
  const f = s.flush()
  check(a.reasoning + b.reasoning + f.reasoning === 'still going</' && f.content === '', 'a stream ending inside an open block flushes as reasoning, including a held partial tag')
}
{
  const r = run(['</think>'])
  check(r.reasoning === '' && r.content === '', 'a bare closing tag alone closes an empty block')
}

// ── The template opened the block in the prompt: only `</think>` arrives ────
const IMPLICIT = ['The user said ', '"hi" again.', '\n</thi', 'nk>\n\nHi! How', "'s it"]
{
  const r = run(IMPLICIT)
  check(r.reasoning === 'The user said "hi" again.' && r.content === "Hi! How's it",
    `a bare </think> moves everything before it into reasoning (${JSON.stringify({ reasoning: r.reasoning, content: r.content })})`)
  check(r.rewrites === 1 && r.inline, 'it does so as one rewrite, and marks the stream inline')
}
{
  const r = run(IMPLICIT, { startInside: true })
  check(r.steps[0].reasoning === 'The user said ' && r.steps[0].content === '', 'startInside streams reasoning from the first delta')
  check(r.reasoning === 'The user said "hi" again.\n' || r.reasoning === 'The user said "hi" again.',
    `startInside: reasoning (${JSON.stringify(r.reasoning)})`)
  check(r.content === "Hi! How's it" && r.rewrites === 0, 'startInside: the answer after </think>, no rewrite needed')
}
{
  const r = run(['<think>\n', 'x', '</think>Y'], { startInside: true })
  check(r.reasoning === 'x' && r.content === 'Y', 'startInside still strips an explicit <think> at the head')
}
{
  const r = run(['Hello', ' there'], { startInside: true })
  check(r.reasoning === '' && r.content === 'Hello there' && r.rewrites === 1 && !r.inline,
    'startInside with no </think> ever: the flush hands it all back as the answer')
}
{
  const r = run(['Use ', '<think> and </think>', ' tags'])
  check(r.reasoning === '' && r.content === 'Use <think> and </think> tags' && r.rewrites === 0,
    'an answer that quotes both tags is left alone')
}
{
  const r = run(['Hello ', '<th', 'ink>x', '</think>'])
  check(r.reasoning === '' && r.content === 'Hello <think>x</think>', 'a quoted <think> split across deltas still marks the answer as quoting')
}
{
  const s = M.createThinkSplitter()
  const a = s.push('<')
  const b = s.push('b>')
  check(a.content === '' && a.reasoning === '', 'a lone "<" is held: it could still be the opening tag')
  check(b.content === '<b>', `a prefix that fails releases the whole buffer as content (${JSON.stringify(b.content)})`)
}
{
  const r = run(['  ', 'Hi'])
  check(r.content === '  Hi', 'leading whitespace before plain text is kept')
}
{
  const r = run(['<think>', '\n', 'a</think>B'])
  check(r.reasoning === 'a' && r.content === 'B', 'the template newline in its own delta after <think> is dropped')
}

done()
