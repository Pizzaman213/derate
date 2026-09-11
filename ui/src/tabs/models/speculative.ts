/** The Speculative field's decisions, with no React in them.
 *
 *  Split out for the same reason `state/routes.ts` is: these are the parts that
 *  can be wrong in ways a typecheck cannot see, and `speculative.check.mjs`
 *  imports THIS rather than mirroring it. The check harness bundles with
 *  esbuild's `platform: 'neutral'`, so a module reaching for React could not be
 *  loaded there at all — which is the enforcement, not just the convention.
 *
 *  Every function here is pure over wire data. Nothing computes a throughput,
 *  a memory cost or whether a head can be launched; all three come from the
 *  coordinator, which is the only side that can answer them.
 */
import { ownerOf } from './owner'
import type { SpeculativeHead, SpeculativeOption } from '../../api/types'
import type { SpecRef } from '../../state/routes'

/** The value the method picker carries for "a head I will name myself".
 *
 *  Not a method name: which method loads a head is read off the head's own
 *  architecture by the coordinator, never chosen here. Picking this only means
 *  "I am about to give you a repository". */
export const EXTERNAL = '__external__'

/** What a head drafts before the coordinator has said. Replaced by the real
 *  figure the moment it answers — it is `HEAD_DEFAULT_TOKENS` server-side, and
 *  this copy exists only so the first request has a number in it. */
export const HEAD_TOKENS = 3

/** Bounded by what the option's own source says it can draft.
 *
 *  Applied on the way out of the number input rather than trusted from it:
 *  `max` on a number field is advisory, and typing past it still fires the
 *  change. An MTP checkpoint carries a fixed number of heads and drafting past
 *  them means looping one, which is a claim about the runtime that nothing
 *  here has verified — so the ceiling is a real bound, not a nicety.
 */
export function clampTokens(n: number, max: number): number {
  if (!Number.isFinite(n)) return 1
  const floored = Math.max(1, Math.floor(n))
  return max > 0 ? Math.min(floored, max) : floored
}

/** A scanned head as a `?spec=` selection.
 *
 *  One place, because three call sites make it — the checkbox, the browser row
 *  and the n control — and a fourth spelling of it is how one of them comes to
 *  send a different launch than it showed.
 *
 *  **The default n is the head's MAXIMUM, not its `default_tokens`, and that is
 *  the fix for exactly that class of bug.** The ceiling on the row was computed
 *  by the server at `max_tokens` — it is the k the head was ranked at — so
 *  applying `default_tokens` instead would tick a box advertising 150 tok/s at
 *  n=8 and launch at n=3. Whatever number is displayed and whatever number is
 *  sent have to be the same number.
 */
export function refFor(head: SpeculativeHead, tokens?: number): SpecRef {
  // `||`, not `??`: a zero maximum means the source did not state one, not
  // that this head drafts nothing. `??` keeps the 0 and clamps to a draft of
  // one token, silently turning a missing field into the slowest legal launch.
  const wanted = tokens ?? (head.max_tokens || head.default_tokens || HEAD_TOKENS)
  return {
    method: head.method,
    tokens: clampTokens(wanted, head.max_tokens),
    // A method the CHECKPOINT declares has no separate repository — the draft
    // is inside the model, and `model_id` on that row is the target itself.
    // Sending it as a head would ask the coordinator to resolve the model as
    // its own draft.
    ...(head.source === 'head' ? { model: head.model_id } : {}),
  }
}

/** The offered option that the current selection refers to, or null.
 *
 *  A named head is matched by `source`, not by method: the operator picks a
 *  repository and the coordinator answers with whatever method its class
 *  turned out to declare, so matching on the method would miss exactly when
 *  the answer was interesting.
 */
export function describes(
  options: SpeculativeOption[],
  chosen: SpecRef | null,
): SpeculativeOption | null {
  if (!chosen) return null
  return (
    options.find((o) =>
      chosen.model ? o.source === 'head' : o.method === chosen.method,
    ) ?? null
  )
}

/** What choosing a row in the method picker means.
 *
 *  `spec` is what to send; `external` is whether the picker stays on the "a
 *  head I will name" row. **They are independent, and that is the whole point
 *  of this function.** Selecting the external row used to send a placeholder
 *  `eagle3` with no head — which the coordinator correctly refused, leaving
 *  `result` null, and since this control renders inside the Verdict card that
 *  result builds, the control unmounted the instant you selected it. A mode is
 *  not a request; only a named head is.
 */
export function methodChoice(
  next: string,
  options: SpeculativeOption[],
): { spec: SpecRef | null; external: boolean } {
  if (next === EXTERNAL) return { spec: null, external: true }
  // A blocked row is an option derate found and cannot price. It is selectable
  // so it can explain itself, and selecting it means "off" — sending it would
  // ask for a launch the fit gate has already said it cannot budget.
  if (!next || next.endsWith(':blocked')) return { spec: null, external: false }
  const option = options.find((o) => o.method === next)
  return {
    spec: { method: next, tokens: option?.default_tokens ?? 1 },
    external: false,
  }
}

/** Recommended rows first, then the rest of the ranking in its own order.
 *
 *  Stable within each group: the server ranked them and re-sorting here would
 *  substitute a client-side opinion for the one the payload's `caveat` is
 *  about.
 */
export function orderHeads(heads: SpeculativeHead[]): SpeculativeHead[] {
  return [...heads.filter((h) => h.recommended), ...heads.filter((h) => !h.recommended)]
}


/** How much faster, at most — `6.1` for "up to 6.1x".
 *
 *  **A multiple rather than a tok/s, and that is a correctness fix.** The scan
 *  ranks every head against one baseline: `predict_decode_tps` at the node's
 *  bandwidth, with no context and no concurrency. The Verdict card three lines
 *  below is the fit gate's answer for the ACTUAL plan — this quantization, this
 *  context, this placement — so its baseline is a different number, and the
 *  screen was showing "up to 150 tok/s" directly above "speculating 10 to 90
 *  tok/s" for the same head.
 *
 *  The ratio is the part that does not depend on the assumption. The server's
 *  ceiling is `base * (k + 1) / speculative_overhead(k, ratio)`, so dividing by
 *  `base` cancels it exactly: bandwidth, context and placement all drop out,
 *  and 150/24.7 and 90.2/15.0 are the same 6.1x. So this control states the
 *  multiple, which is true under either baseline, and the card keeps sole
 *  ownership of tok/s, which is only true under one.
 */
export function speedup(ceiling: number, baseline?: number): number | null {
  if (!baseline || baseline <= 0 || !Number.isFinite(ceiling)) return null
  const x = ceiling / baseline
  return Number.isFinite(x) && x > 0 ? x : null
}


/** What to call a row in the ranked list.
 *
 *  A published head is its repository. A method the CHECKPOINT declares is the
 *  model itself, correctly — an MTP module ships inside the checkpoint, so
 *  naming the target names where the draft actually is. A method that needs no
 *  model at all is neither: ngram's row carried the target's id, which reads as
 *  though the model were its own draft, and it is the one method that loads no
 *  weights of any kind. It is named by its method.
 */
export function headLabel(head: SpeculativeHead): string {
  return head.source === 'method' ? head.method : head.model_id
}


/** The publisher to draw a mark for, or `''` for none.
 *
 *  **Not simply `ownerOf(head.model_id)`.** A method that needs no model at all
 *  still carries the TARGET's id on its row — that is how the server addresses
 *  it — so ngram would have drawn the target's publisher beside itself, as
 *  though Qwen had published a string-matching heuristic. `headLabel` already
 *  refuses to *name* it after the target; this refuses to badge it too, and
 *  both read the same `source` field so they cannot drift apart.
 *
 *  A checkpoint's own module keeps the target's mark, correctly: an MTP module
 *  ships inside the checkpoint, so it really was published by those people.
 */
export function markOwner(head: SpeculativeHead): string {
  return head.source === 'method' ? '' : ownerOf(head.model_id)
}
