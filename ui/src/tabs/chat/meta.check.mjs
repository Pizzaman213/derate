// Checks the line under every answer in the chat transcript: tabs/chat/meta.ts.
//
// Nothing here is visible to `tsc`. `metaLine` returns a string whichever
// figures it printed, dropped or invented, and the whole point of the line is
// which ones those are. Four classes of bug, one per section:
//
//   1. A FIGURE THAT WAS NEVER MEASURED, printed as a number. The dash rule is
//      the oldest thing in this file and the easiest to lose to a `?? 0`.
//   2. A RATE THAT DISAGREES WITH THE REST OF THE PRODUCT. `decodeTps` is
//      `tokens / (elapsed - ttft)` because that is what
//      `gateway/stats.py::TargetStats.complete` folds into `decode_tps`, and
//      the deployment inspector prints that one as "tok/s per stream" for the
//      same request. Two definitions of one number is the drift this repo
//      keeps finding; the pinned pair below is what stops it here.
//   3. A RATE FROM A WINDOW THAT MEASURES NOTHING. An upstream that ships a
//      whole completion in one frame has `elapsed == ttft`, and dividing a
//      real token count by that prints five figures of tok/s.
//   4. A ROW THAT SHOULD NOT EXIST AT ALL. Both audio endpoints have no
//      tokens, no ttft and no elapsed, and `ttft — ms · — s · — tok` reads as
//      three failed measurements rather than as a request that never had them.
//
//   cd ui && node src/tabs/chat/meta.check.mjs
import { load, report } from '../../check/harness.mjs'

const M = await load(import.meta.url, './meta.ts')
const { check, note, done } = report()

/** A finished streaming turn. Fields are overridden per case. */
const turn = (over = {}) => ({
  model: 'Qwen3-4B-AWQ',
  requestId: 'r-076dd2c900f1',
  ttftMs: 1985,
  elapsedMs: 2600,
  completionTokens: 40,
  tokensEstimated: true,
  reasoningMs: null,
  stopped: false,
  ...over,
})

// ── 1. the dash rule ─────────────────────────────────────────────────────────

check(
  M.metaLine(null, 'r-abc') === 'r-abc',
  'a turn that only got as far as a request id prints the id alone',
)
check(
  M.metaLine(turn({ ttftMs: null }), null).includes('ttft — ms'),
  'an unmeasured ttft is an em dash',
)
check(
  M.metaLine(turn({ completionTokens: null }), null).includes('— tok'),
  'an unmeasured token count is an em dash',
)
check(
  !M.metaLine(turn({ completionTokens: null }), null).includes('0 tok'),
  'a missing token count never reads as zero tokens',
)
check(
  M.metaLine(turn({ tokensEstimated: false }), null).includes('40 tok'),
  'a usage-block count prints bare',
)
check(
  M.metaLine(turn(), null).includes('40 (est.) tok'),
  'a count of delta frames says so',
)

// ── 2. the rate is the product's rate ────────────────────────────────────────

// The line this was written from, verbatim off the screen:
//   Qwen3-4B-AWQ · r-076dd2c900f1 · ttft 1985 ms · 2.6 s · 40 (est.) tok
// 40 tokens over the 615 ms that followed the first one is 65 tok/s. The
// whole-request reading of the same turn is 40/2.6 = 15, and it is wrong here:
// it falls when a queue is long rather than when decode is slow, and ttft is
// already its own figure three fields to the left.
const observed = M.metaLine(turn(), 'r-076dd2c900f1')
note(observed)
check(
  observed === 'Qwen3-4B-AWQ · r-076dd2c900f1 · ttft 1985 ms · 2.6 s · 40 (est.) tok · 65 tok/s',
  'the observed turn reads 65 tok/s',
)
check(
  Math.abs(M.decodeTps(turn()) - 40000 / 615) < 1e-9,
  'the rate is tokens over the window after the first token',
)
check(
  M.decodeTps(turn({ ttftMs: 0 })) === (40 * 1000) / 2600,
  'with no wait, the decode window is the whole request',
)

// A slow decode must not round into the zero this UI reserves for a measured
// zero -- a 0.4 tok/s answer is the readout that explains a screen that looks
// hung, and `0 tok/s` claims nothing came out at all.
const slow = M.metaLine(turn({ completionTokens: 4, elapsedMs: 11985 }), null)
check(slow.includes('0.4 tok/s'), `a sub-1 rate keeps its decimal (${slow})`)
check(
  M.metaLine(turn({ completionTokens: 400, elapsedMs: 3985 }), null).includes('200 tok/s'),
  'a rate over 10 tok/s prints whole',
)

// ── 3. no window, no rate ────────────────────────────────────────────────────

// Not a dash: every input to the rate is printed to its left, so `— tok/s`
// would read as a measurement that failed rather than as arithmetic that was
// not done.
for (const [name, over] of [
  ['one frame carried everything', { elapsedMs: 1985 }],
  ['two frames a moment apart', { elapsedMs: 1986 }],
  ['the stream ended before the elapsed clock', { elapsedMs: 1000 }],
  ['no first token was ever seen', { ttftMs: null }],
  ['the turn was never timed', { elapsedMs: null }],
  ['nothing was counted', { completionTokens: null }],
  ['the answer was empty', { completionTokens: 0 }],
]) {
  check(M.decodeTps(turn(over)) === null, `no rate when ${name}`)
  check(!M.metaLine(turn(over), null).includes('tok/s'), `...and none is printed (${name})`)
}
check(
  M.MIN_DECODE_MS > 0,
  'the floor on the decode window is a positive number of milliseconds',
)

// ── 4. a turn with no token figures at all ───────────────────────────────────

// Speech and transcription. The test is on the figures, not on the modality,
// so it survives a fourth endpoint.
const audio = turn({ ttftMs: null, elapsedMs: null, completionTokens: null })
check(
  M.metaLine(audio, 'r-audio') === 'Qwen3-4B-AWQ · r-audio',
  'a turn with no token-denominated figures prints the model and the id alone',
)
check(
  M.metaLine({ ...audio, stopped: true }, 'r-audio') === 'Qwen3-4B-AWQ · r-audio · stopped',
  '...and still says when it was stopped',
)
check(
  M.metaLine(turn({ stopped: true }), null).endsWith('stopped'),
  'stopped is the last field on a measured turn, after the rate',
)
// A stopped turn is a real partial answer, and the tokens that did arrive were
// decoded at a real rate. Dropping the figure would make Stop look like a
// failure rather than a choice.
check(
  M.metaLine(turn({ stopped: true }), null).includes('65 tok/s'),
  'a stopped turn still reports what it decoded',
)

done()
