// requires: coordinator -- the cases live data can reach; the rest are inline
// Checks `spend/rows.ts` — the Spend screen's arithmetic — against the live
// coordinator on :8088, plus the cases live data cannot reach.
//
// The sibling of `tabs/models/rows.check.mjs`, and for the same reason: there
// is no test runner in `ui/`, and every interesting thing this module does is
// about what a real payload means rather than whether it compiles. A green
// `tsc` says `spend_today_usd` is a number. It cannot say that the number is
// knowledge, and that distinction is the whole module.
//
//   cd ui && node src/tabs/spend/rows.check.mjs
//   DERATE_CHECK_ORIGIN=http://localhost:18088 node src/tabs/spend/rows.check.mjs
import { build } from 'esbuild'
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

const dir = mkdtempSync(join(tmpdir(), 'spend-'))
const out = join(dir, 'rows.mjs')
await build({
  entryPoints: [fileURLToPath(new URL('rows.ts', import.meta.url))],
  bundle: true, format: 'esm', outfile: out, logLevel: 'silent',
})
const R = await import(out)

const ORIGIN = process.env.DERATE_CHECK_ORIGIN ?? 'http://localhost:8088'
let fail = 0
const check = (ok, msg) => { console.log(`${ok ? 'PASS' : 'FAIL'}  ${msg}`); if (!ok) fail++ }
// Context worth printing that is not a claim about correctness.
const note = (msg) => console.log(`      ${msg}`)

// A provider row with the shape `providers/serialization.py` emits. Only the
// keys this module reads; everything else on the wire is irrelevant here.
const prov = (o) => ({
  provider_id: 'p', models: [],
  spend_today_usd: 0, requests_today: 0,
  unpriced_requests_today: 0, metered_requests_today: 0,
  tokens_today: { input: 0, output: 0 },
  ...o,
})

// ---------------------------------------------------------------------------
// 1. The bug this module exists for: $0.00 that is not a zero
// ---------------------------------------------------------------------------

// A provider that served nothing really did spend nothing.
{
  const s = R.cloudSpend(prov({ requests_today: 0 }))
  check(s.usd === 0 && s.basis === 'idle', 'no requests today is a real $0.00, not unknown')
}

// A provider that served 200 requests through models it publishes no price for
// reports the identical $0.00 on the wire. It is not the same fact.
{
  const s = R.cloudSpend(prov({ requests_today: 200, unpriced_requests_today: 200 }))
  check(s.usd === null, 'all-unpriced traffic reports unknown spend, not $0.00')
  check(s.basis === 'unpriced', 'and says why')
  check(R.money(s.usd) === '—', 'which renders as an em dash')
}

// The port that does no accounting at all nulls the whole group.
{
  const s = R.cloudSpend(prov({ requests_today: null, spend_today_usd: null }))
  check(s.usd === null && s.basis === 'unknown', 'a non-accounting port is unknown, not idle')
}

// Partly unpriced: the figure is real but is a floor, and must say so.
{
  const s = R.cloudSpend(prov({
    requests_today: 10, unpriced_requests_today: 4, metered_requests_today: 6,
    spend_today_usd: 1.5,
  }))
  check(s.usd === 1.5 && s.floor === true, 'partly-unpriced spend is a lower bound')
  check(R.money(s.usd, s.floor) === '≥ $1.50', 'and renders with the bound marker')
}

// ---------------------------------------------------------------------------
// 2. Metered vs estimated — a charge and a forecast are different claims
// ---------------------------------------------------------------------------
{
  const metered = R.cloudSpend(prov({
    requests_today: 5, metered_requests_today: 5, spend_today_usd: 0.4,
  }))
  check(metered.basis === 'metered', 'every request priced by the provider is metered')

  const estimated = R.cloudSpend(prov({
    requests_today: 5, metered_requests_today: 0, spend_today_usd: 0.4,
  }))
  check(estimated.basis === 'estimated', 'none priced by the provider is estimated')

  const mixed = R.cloudSpend(prov({
    requests_today: 5, metered_requests_today: 2, spend_today_usd: 0.4,
  }))
  check(mixed.basis === 'mixed', 'some of each is mixed')

  // A record written before metering existed has the key absent, not zero.
  const legacy = R.cloudSpend(prov({
    requests_today: 5, metered_requests_today: undefined, spend_today_usd: 0.4,
  }))
  check(legacy.basis === 'estimated', 'a pre-metering record reads as estimated, not a crash')
}

// A free model charging nothing is knowledge. Only silence is a gap.
{
  const s = R.cloudSpend(prov({
    requests_today: 3, metered_requests_today: 3, spend_today_usd: 0,
  }))
  check(s.usd === 0 && s.basis === 'metered', 'a metered $0.00 is priced, not unpriced')
}

// ---------------------------------------------------------------------------
// 3. The fleet total drops nothing silently
// ---------------------------------------------------------------------------
{
  const f = R.fleetSpend([
    prov({ provider_id: 'openrouter', requests_today: 10, metered_requests_today: 10, spend_today_usd: 2 }),
    prov({ provider_id: 'groq', requests_today: 50, unpriced_requests_today: 50 }),
  ])
  check(f.usd === 2, 'the total is what is known')
  check(f.floor === true, 'and is marked a floor because groq served 50 unpriced')
  check(f.blindProviders.join() === 'groq', 'naming the provider whose bill is missing')
  check(f.unpricedRequests === 50, 'and how many requests it was')
  check(
    R.spendCaption(false, f) === 'spent today · excludes local generation and unpriced cloud models',
    'the caption lists BOTH exclusions, not just the electricity one',
  )
  check(
    R.spendCaption(true, f) === 'spent today · excludes unpriced cloud models',
    'and drops the electricity one once a rate is set',
  )
  const notes = R.spendNotes(false, f)
  check(notes.length === 2, 'both notes render')
  check(
    notes[1] === '50 requests went to models groq publishes no price for',
    'the second names the traffic we could not price',
  )
  check(
    R.basisNote(f, true).includes('what your provider reports it charged'),
    'the basis note says metered spend is a charge',
  )
}

// Nobody accounts for anything: the total stays null rather than seeding to 0.
{
  const f = R.fleetSpend([prov({ requests_today: null, spend_today_usd: null })])
  check(f.usd === null, 'a fleet with no accounting totals to unknown')
  check(f.blindProviders.length === 0, 'and is not reported as unpriced traffic — there may be none')
  check(R.spendCaption(true, f) === 'spent today', 'so the caption claims no exclusion')
  check(R.basisNote(f, false) === null, 'and there is no basis to describe')
}

// An empty fleet is not $0.00 either.
check(R.fleetSpend([]).usd === null, 'no providers at all totals to unknown')

// ---------------------------------------------------------------------------
// 4. Published price of what a provider actually serves
// ---------------------------------------------------------------------------
{
  const m = (o) => ({ input_cost_per_mtok: null, output_cost_per_mtok: null, ...o })
  check(R.publishedRange([]) === null, 'no models is no price')
  check(R.publishedRange([m({})]) === null, 'an unpriced model is no price')
  check(
    R.formatRange(R.publishedRange([m({ output_cost_per_mtok: 0.12 })])) === '$0.120',
    'one served model shows its exact rate',
  )
  check(
    R.formatRange(R.publishedRange([
      m({ output_cost_per_mtok: 0.12 }), m({ output_cost_per_mtok: 6 }),
    ])) === '$0.120–$6.000',
    'several disagreeing models show a range, never an invented midpoint',
  )
  // Output, matching gateway/targets.py::remote_cost_per_mtok — decode
  // dominates a serving workload, so the output rate is the single number.
  check(
    R.formatRange(R.publishedRange([m({ input_cost_per_mtok: 1, output_cost_per_mtok: 9 })])) === '$9.000',
    'the output rate is the one shown',
  )
  check(
    R.formatRange(R.publishedRange([m({ input_cost_per_mtok: 1 })])) === '$1.000',
    'falling back to input only when output is unpublished',
  )
  // A $0 model is a price. It must not read the same as an unpriced one.
  check(
    R.formatRange(R.publishedRange([m({ output_cost_per_mtok: 0 })])) === '$0.000',
    'a free model publishes $0.000, which is not an em dash',
  )
}

// Output tokens only, so the tile compares like with like against a local
// target's `counters.total_tokens` (gateway/proxy.py fills it from completions).
{
  check(R.cloudTokens(prov({ tokens_today: { input: 900, output: 100 } })) === 100,
    'cloud tokens generated is the output half, not the sum')
  check(R.cloudTokens(prov({ tokens_today: null })) === null, 'and null when nobody counted')
}

// ---------------------------------------------------------------------------
// 5. Against the live coordinator
// ---------------------------------------------------------------------------
// This used to catch the unreachable case, print SKIP and carry on to
// `process.exit(fail === 0 ? 0 : 1)` -- so a coordinator that was simply not
// running produced the same green as one whose every number checked out. The
// runner probes for the coordinator now (`// requires: coordinator`) and skips
// the whole file, in its own column, when there is none. Getting this far
// means one answered, so a failure here is a real one.
const live = await (await fetch(`${ORIGIN}/api/providers`)).json()
{
  const provs = Array.isArray(live) ? live : (live.providers ?? [])
  // A coordinator with no providers configured is a real state, not a broken
  // one -- it is what a fresh install is -- so it gets checked rather than
  // demanded away. This used to assert `provs.length > 0`, which turned "the
  // operator has not added a provider yet" into a test failure, and then read
  // provs[0] anyway and crashed.
  note(`${ORIGIN} has ${provs.length} provider(s) to read`)
  if (provs.length === 0) {
    const empty = R.fleetSpend([])
    check(empty.usd === null || empty.usd === 0,
      `an empty fleet reports ${JSON.stringify(empty.usd)}, not an invented number`)
  }
  for (const p of provs) {
    // The keys this module reads must actually be on the wire, with the shapes
    // it assumes. A rename server-side would otherwise show up as a silent
    // em dash rather than as a failure.
    check('metered_requests_today' in p,
      `${p.provider_id}: the wire carries metered_requests_today`)
    check(p.tokens_today == null || typeof p.tokens_today === 'object',
      `${p.provider_id}: tokens_today is {input,output}, as api/types.ts now says`)
    const s = R.cloudSpend(p)
    check(s.usd !== null || p.requests_today == null || p.unpriced_requests_today >= p.requests_today,
      `${p.provider_id}: spend is only withheld when it is genuinely unknown`)
    // `/api/providers` is the SERVABLE view (internal_api.py `_servable`), so
    // this range is the price of what the operator switched on -- never the
    // catalogue's full spread, which lives at /api/providers/{id}/models.
    const range = R.publishedRange(p.models)
    const priced = (p.models ?? []).filter(
      (mm) => mm.output_cost_per_mtok != null || mm.input_cost_per_mtok != null)
    check(priced.length === 0 ? range === null : range !== null,
      `${p.provider_id}: ${priced.length} priced of ${p.models?.length ?? 0} served -> ${R.formatRange(range)}`)
    check(range === null || range.low <= range.high, `${p.provider_id}: the range is ordered`)
    check((p.models?.length ?? 0) === (p.model_count ?? p.models?.length ?? 0),
      `${p.provider_id}: models[] matches model_count, so it is the servable view`)
  }
  if (provs.length > 0) {
    const catalogue = await (await fetch(`${ORIGIN}/api/providers/${provs[0].provider_id}/models`)).json()
    check(catalogue.length >= (provs[0].models?.length ?? 0),
      `the catalogue endpoint holds >= the servable list (${catalogue.length} vs ${provs[0].models?.length ?? 0})`)
  }

  const f = R.fleetSpend(provs)
  console.log(`      fleet: ${R.money(f.usd, f.floor)} — ${R.spendCaption(false, f)}`)
  for (const n of R.spendNotes(false, f)) console.log(`      note: ${n}`)
}

console.log(fail === 0 ? '\nall checks passed' : `\n${fail} check(s) failed`)
process.exit(fail === 0 ? 0 : 1)
