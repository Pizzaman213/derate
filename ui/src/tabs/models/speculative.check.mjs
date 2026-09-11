// The Speculative field's decisions, checked against `speculative.ts` itself
// rather than a mirror of it.
//
//   node src/tabs/models/speculative.check.mjs
//
// Every class of bug here has actually shipped in this control. The one worth
// naming twice is `methodChoice`: this field renders INSIDE the Verdict card
// that the plan response builds, so a selection that makes the coordinator
// refuse takes `result` to null and unmounts the control that made it. Twice
// now, selecting a mode has destroyed the control. That is what the first
// group below is for.

import { load, report } from '../../check/harness.mjs'

const M = await load(import.meta.url, './speculative.ts')
const { check, done } = report()

const head = (over = {}) => ({
  model_id: 'org/Qwen3-4B_eagle3',
  method: 'eagle3',
  max_tokens: 8,
  draft_bytes: 4e8,
  ceiling_tps: 416,
  recommended: false,
  source: 'head',
  ...over,
})

const option = (over = {}) => ({
  method: 'ngram',
  default_tokens: 5,
  max_tokens: 10,
  draft_params: 0,
  draft_bytes: 0,
  source: 'method',
  declared_by: '',
  note: '',
  launchable: true,
  ...over,
})

// ── a mode is not a request ─────────────────────────────────────────────────

const external = M.methodChoice(M.EXTERNAL, [option()])
check(external.spec === null, 'choosing "a head I will name" sends nothing')
check(external.external === true, '...but the picker does enter the mode')

const off = M.methodChoice('', [option()])
check(off.spec === null && off.external === false, 'choosing "off" sends nothing and leaves the mode')

const blocked = M.methodChoice('dspark:blocked', [option({ method: 'dspark', launchable: false })])
check(
  blocked.spec === null,
  'a blocked option is selectable to explain itself and sends nothing',
)

const picked = M.methodChoice('ngram', [option()])
check(picked.spec?.method === 'ngram', 'choosing a real method sends it')
check(picked.spec?.tokens === 5, "...at the option's own default, not a constant")
check(picked.external === false, '...and leaves the external mode')
check(
  M.methodChoice('mtp', []).spec?.tokens === 1,
  'a method with no matching option still sends a legal token count',
)
check(
  !('model' in (picked.spec ?? {})),
  'a built-in method never carries a head repository',
)

// ── n is bounded by what the source says it can draft ───────────────────────

check(M.clampTokens(20, 8) === 8, 'n is clamped down to the maximum')
check(M.clampTokens(0, 8) === 1, 'n is clamped up to one — zero drafts nothing')
check(M.clampTokens(-4, 8) === 1, 'a negative n is clamped up, not negated')
check(M.clampTokens(3.7, 8) === 3, 'a fractional n floors — a draft is whole tokens')
check(M.clampTokens(NaN, 8) === 1, 'an emptied input is one, not NaN in the URL')
check(M.clampTokens(99, 0) === 99, 'an unknown maximum does not clamp to zero')

// ── refFor: one spelling of a selection ─────────────────────────────────────

check(M.refFor(head()).model === 'org/Qwen3-4B_eagle3', 'a hub head carries its repository')
check(M.refFor(head()).tokens === 8, "...and defaults to the head's own maximum")
// The ceiling on the row was computed by the server AT `max_tokens`. Applying
// `default_tokens` instead ticked a box advertising 150 tok/s at n=8 and
// launched at n=3 — the displayed number and the sent number must be one.
check(
  M.refFor(head({ default_tokens: 3, max_tokens: 8 })).tokens === 8,
  'the n applied is the k the ceiling was ranked at, not default_tokens',
)
check(
  M.refFor(head({ default_tokens: 3, max_tokens: 0 })).tokens === 3,
  '...falling back to default_tokens only when there is no maximum',
)
check(M.refFor(head(), 40).tokens === 8, 'an over-large explicit n is still clamped')
// `mtp` ships INSIDE the checkpoint, so its row names the target. Sent as a
// head it would ask the coordinator to resolve the model as its own draft.
const builtin = M.refFor(head({ source: 'checkpoint', method: 'mtp', model_id: 'Qwen/Qwen3-Next' }))
check(builtin.model === undefined, "a checkpoint's own method carries no head repository")
check(builtin.method === 'mtp', '...but does carry its method')

// ── which offered option a selection is about ───────────────────────────────

const opts = [
  option({ method: 'ngram', source: 'method' }),
  option({ method: 'eagle3', source: 'head', note: 'the named head' }),
]
check(M.describes(opts, null) === null, 'nothing selected describes nothing')
check(
  M.describes(opts, { method: 'ngram', tokens: 5 })?.method === 'ngram',
  'a built-in selection is matched by method',
)
// Matched by `source`, not by method, and the difference is load-bearing: the
// operator names a repository and the coordinator answers with whatever method
// its class turned out to declare. Matching on method misses exactly then.
check(
  M.describes(opts, { method: 'eagle', tokens: 3, model: 'org/x' })?.note === 'the named head',
  'a named head is matched by source even when the method came back different',
)
check(
  M.describes([option({ method: 'ngram' })], { method: 'mtp', tokens: 2 }) === null,
  'a method this checkpoint does not offer describes nothing rather than the wrong row',
)

// ── the ranking's order is the server's ─────────────────────────────────────

const ranked = [
  head({ model_id: 'a', ceiling_tps: 500 }),
  head({ model_id: 'b', ceiling_tps: 400, recommended: true }),
  head({ model_id: 'c', ceiling_tps: 300 }),
  head({ model_id: 'd', ceiling_tps: 200, recommended: true }),
]
const ordered = M.orderHeads(ranked)
check(
  ordered.map((h) => h.model_id).join('') === 'bdac',
  'recommended rows lead, and each group keeps the order the server ranked it in',
)
check(ordered.length === ranked.length, 'ordering drops nothing')

// ── the multiple, not the tok/s ─────────────────────────────────────────────
//
// The scan ranks every head against `predict_decode_tps` with no context and
// no concurrency; the Verdict card three lines below answers for the actual
// plan. So the screen showed "up to 150 tok/s" directly above "speculating 10
// to 90 tok/s" for one head. The ratio is what survives — the server's ceiling
// is base*(k+1)/overhead, so dividing by base cancels it exactly.

check(M.speedup(150, 24.7).toFixed(1) === '6.1', "the scan's own numbers give 6.1x")
check(M.speedup(90.2, 15.0).toFixed(1) === '6.0', '...and the card\'s give the same 6.0x')
check(M.speedup(150, 0) === null, 'a zero baseline is no multiple rather than Infinity')
check(M.speedup(150, undefined) === null, 'a missing baseline is no multiple')
check(M.speedup(150, -3) === null, 'a negative baseline is no multiple')
check(M.speedup(Infinity, 10) === null, 'an infinite ceiling is no multiple')

check(
  M.headLabel(head({ source: 'method', method: 'ngram', model_id: 'Qwen/Qwen3-30B-A3B' })) === 'ngram',
  'ngram is named by its method — it loads no draft model, so the target id read as a lie',
)
check(
  M.headLabel(head({ source: 'checkpoint', method: 'mtp', model_id: 'Qwen/Qwen3-Next' })) === 'Qwen/Qwen3-Next',
  "a checkpoint's own module IS in the model, so naming the target is right",
)
check(
  M.headLabel(head({ source: 'head', model_id: 'org/x_eagle3' })) === 'org/x_eagle3',
  'a published head is its repository',
)

// ── whose logo goes on the row ──────────────────────────────────────────────

const O = await load(import.meta.url, './owner.ts')

check(O.ownerOf('lmsys/SGLang-EAGLE3-Qwen3-30B') === 'lmsys', 'the publisher is the half before the slash')
check(O.repoOf('lmsys/SGLang-EAGLE3-Qwen3-30B') === 'SGLang-EAGLE3-Qwen3-30B', 'the repository is the half after it')
// `''` is not "unidentified" — `gpt2` genuinely has no publisher, and the
// difference is what decides whether a tile is drawn at all.
check(O.ownerOf('gpt2') === '', 'a bare repository has no publisher')
check(O.repoOf('gpt2') === 'gpt2', '...and is entirely its own name')
check(O.ownerOf('/leading') === '', 'a leading slash names no publisher')
check(O.ownerOf('a/b/c') === 'a', 'only the first segment is the publisher')

// The bug this rule exists for: a method that needs no model still carries the
// TARGET's id on its row, so the plain split badged ngram with Qwen's logo —
// as though Qwen had published a string-matching heuristic.
check(
  M.markOwner(head({ source: 'method', method: 'ngram', model_id: 'Qwen/Qwen3-30B-A3B' })) === '',
  'ngram draws no mark, although its row carries the target id',
)
check(
  M.markOwner(head({ source: 'checkpoint', method: 'mtp', model_id: 'deepseek-ai/DeepSeek-V3' })) === 'deepseek-ai',
  "a checkpoint's own MTP module keeps the target's mark — it really did ship in their weights",
)
check(
  M.markOwner(head({ source: 'head', model_id: 'lmsys/SGLang-EAGLE3' })) === 'lmsys',
  'a published head is marked with its own publisher',
)
// The two must not drift: whatever is not named after the target is not
// badged with the target's publisher either.
for (const src of ['method', 'checkpoint', 'head']) {
  const h = head({ source: src, method: 'ngram', model_id: 'Qwen/Qwen3-30B-A3B' })
  check(
    (M.headLabel(h) === h.model_id) === (M.markOwner(h) !== ''),
    `naming and badging agree for source=${src}`,
  )
}

done()
