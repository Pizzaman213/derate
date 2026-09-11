import type { ChatTurnMeta } from '../../api/types'
import { fmt } from '../../format'

/** Below this, every token arrived together.
 *
 *  The rate divides by the window AFTER the first token, so an upstream that
 *  ships a whole completion in one frame -- or in two, a few hundred
 *  microseconds apart -- divides a real token count by a window that measures
 *  nothing, and prints five figures of tok/s for a request nobody decoded
 *  quickly. That is not a slow number to be smoothed; it is the absence of a
 *  decode window, and it prints as no rate at all rather than as a fast one. */
export const MIN_DECODE_MS = 5

/** Tokens per second for one finished turn, or `null` when the turn does not
 *  have one.
 *
 *  This is `completionTokens / (elapsed - ttft)`: the tokens over the window
 *  after the first one arrived, which is the SAME arithmetic
 *  `gateway/stats.py::TargetStats.complete` folds into `decode_tps` from
 *  `usage.tokens / usage.decode_s`. Deliberately the same and not merely
 *  similar -- this line and the deployment inspector's "tok/s per stream" are
 *  two readouts of one request, and a second definition written here is how
 *  they would come to disagree about it. Time to the first token is already
 *  its own figure on the line; folding it in here would give a rate that falls
 *  when a queue is long rather than when decode is slow.
 *
 *  The count's provenance carries: when `tokensEstimated` says the tokens were
 *  counted delta frames, this is that estimate divided by a measured window,
 *  and the `(est.)` the line prints beside the count governs the rate next to
 *  it too. */
export function decodeTps(m: ChatTurnMeta): number | null {
  const { ttftMs, elapsedMs, completionTokens } = m
  if (ttftMs === null || elapsedMs === null || completionTokens === null) return null
  if (completionTokens <= 0) return null
  const windowMs = elapsedMs - ttftMs
  if (!(windowMs >= MIN_DECODE_MS)) return null
  return (completionTokens * 1000) / windowMs
}

/** The one line under an answer that makes this a control-plane surface rather
 *  than a chat window: what served it, the id of the row that recorded it, and
 *  what it cost in time.
 *
 *  Missing figures print as an em dash, never as zero, and a token count that
 *  came from counting delta frames says so — the gateway makes the same
 *  distinction on its own records and it is not ours to quietly drop.
 *
 *  Split out of `Transcript.tsx` so `meta.check.mjs` can import it: the
 *  verifiers bundle with esbuild's `platform: 'neutral'` and cannot reach a
 *  module that pulls in React, and every rule below is a string this file
 *  decides and `tsc` cannot see. It takes the two fields it reads rather than
 *  a `Turn` for the same reason. */
export function metaLine(meta: ChatTurnMeta | null, requestId: string | null): string {
  const m = meta
  const id = requestId ?? m?.requestId ?? '—'
  if (!m) return id
  // Nothing token-denominated exists on this turn, so none of it is printed.
  //
  // Both audio endpoints land here: a speech turn's real figures are duration
  // and sample rate and `Clip` renders them, and a transcription turn has no
  // rate at all -- the gateway deliberately counts no tokens over a binary
  // body either way. The test is on the figures rather than on the modality
  // so that it stays true for whatever the fourth endpoint turns out to be.
  //
  // This is the dash rule, not an exception to it: an unmeasured figure is a
  // dash, and a figure that does not exist for this kind of request is not a
  // row at all. `ttft — ms · — s · — tok` reads as three failed measurements
  // rather than as a request that was never going to have them.
  if (m.ttftMs === null && m.elapsedMs === null && m.completionTokens === null) {
    const line = [m.model, id].filter(Boolean).join(' · ')
    return m.stopped ? `${line} · stopped` : line
  }
  const tokens =
    m.completionTokens == null
      ? '—'
      : `${m.completionTokens}${m.tokensEstimated ? ' (est.)' : ''}`
  const tps = decodeTps(m)
  const parts = [
    m.model,
    id,
    `ttft ${fmt(m.ttftMs)} ms`,
    `${fmt(m.elapsedMs == null ? null : m.elapsedMs / 1000, 1)} s`,
    `${tokens} tok`,
  ]
  // A rate that does not exist is left out, not dashed: the dash rule is for a
  // figure this request has and we failed to measure, and every input to the
  // rate is already printed to its left -- `— tok/s` beside a ttft, an elapsed
  // and a token count would read as a measurement that failed rather than as
  // arithmetic that was not done.
  //
  // One decimal under 10 tok/s, none above. `fmt`'s fixed-width rule is for a
  // column of readouts that must not jump; this is prose, and the widths here
  // already vary. What it buys is that a genuinely slow decode -- a large
  // model spilling to host memory -- reads `0.4 tok/s` rather than rounding to
  // the `0` that everything else in this UI reserves for a measured zero.
  if (tps !== null) parts.push(`${fmt(tps, tps < 10 ? 1 : 0)} tok/s`)
  if (m.stopped) parts.push('stopped')
  return parts.join(' · ')
}
