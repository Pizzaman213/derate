// The spend window's arithmetic (usage.ts), on literal `/api/usage` rows.
// Hermetic: no coordinator, no browser.
//
// What it pins is the screen's one rule -- a figure nobody reported is null,
// never 0 -- through the folds that could lose it: provider rows summed into
// fleet totals, provider rows folded into one cost per bucket, key rows into a
// breakdown, and the floor marker that says a total leaves unpriced traffic out.

import { load, report } from '../../check/harness.mjs'

const U = await load(import.meta.url, './usage.ts')
const R = await load(import.meta.url, './rows.ts')
const K = await load(import.meta.url, '../keys/rows.ts')
const { check, done } = report()

const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b)

/** One `/api/usage` row with every optional measure unreported. */
const row = (bucket, group, o = {}) => ({
  bucket, group,
  requests: 0, ok: 0, failed: 0, errors: {},
  error_rate: null, fallback_rate: null, cancel_rate: null,
  tokens_in: null, tokens_out: null, reasoning_tokens: null,
  cache_read_tokens: null, cache_write_tokens: null, cache_hit_rate: null, cache_errors: {},
  avg_prompt_tokens: null, avg_output_tokens: null,
  spec_drafts: null, spec_draft_tokens: null, spec_accepted_tokens: null,
  spec_acceptance_rate: null, spec_accepted_per_position: null,
  spec_acceptance_per_position: null, spec_errors: {},
  ttft_ms: null, latency_ms: null, queue_ms: null, tpot_ms: null, tokens_per_s: null,
  cost_usd: null, metered_usd: null, estimated_usd: null,
  unpriced_requests: 0, cost_basis: null,
  ...o,
})

// ── The window ───────────────────────────────────────────────────────────────

check(U.startFor('24h') === '-24h', 'a day asks for -24h')
check(U.startFor('all') === '-400d', 'all asks for the 1d rollups\' whole retention')
check(U.bucketFor('24h') === '1h', 'a day is drawn per hour')
check(U.bucketFor('all') === '1d', 'anything longer per day')

// ── Totals ───────────────────────────────────────────────────────────────────

const B = 86400 * 100
{
  const t = U.windowTotals([
    // `""` is what the wire carries for a local target (the rollups store
    // `provider_id or ""`); a check on null alone passed while every local
    // request on a real coordinator was counted as cloud.
    row(B, { provider: '' }, {
      requests: 3, tokens_in: 1000, cache_read_tokens: 250, cost_usd: 0.5, estimated_usd: 0.5,
      cost_basis: 'estimated',
    }),
    row(B, { provider: 'groq' }, { requests: 2, unpriced_requests: 2, cost_basis: 'unpriced' }),
  ])
  check(t.requests === 5 && t.localRequests === 3 && t.cloudRequests === 2,
    'a null provider is local, a named one cloud')
  check(t.tokensIn === 1000, 'an unreported tokens_in adds nothing rather than zeroing the sum')
  check(t.cacheRead === 250 && t.cachedShare === 0.25, 'cached share is cache reads over input')
  check(t.cost === 0.5 && t.cloudUnpriced === 2 && t.localUnpriced === 0, 'unpriced requests are counted per side')
  check(t.floor === true, 'priced dollars over unpriced requests are a floor')
}
{
  const t = U.windowTotals([row(B, { provider: 'groq' }, { requests: 4, unpriced_requests: 4, cost_basis: 'unpriced' })])
  check(t.cost === null && t.floor === false, 'all-unpriced traffic is an unknown cost, not $0 and not a floor')
}
{
  const t = U.windowTotals([])
  check(t.requests === 0 && t.cost === null && t.tokensIn === null && t.cachedShare === null,
    'an empty window has 0 requests and nothing else measured')
}

check(eq(U.totalsNotes({ ...U.windowTotals([]), localUnpriced: 1 }),
  ['1 local request unpriced: set a rate for the model under Settings → Pricing']),
'one unpriced local request is singular and names where to fix it')
check(eq(U.totalsNotes({ ...U.windowTotals([]), cloudUnpriced: 50 }),
  ['50 cloud requests went to models the provider publishes no price for']),
'unpriced cloud requests name the cause')
check(U.totalsBasis({ metered: 1.2, estimated: 0.34 }) === 'metered $1.20 · estimated $0.3400', 'both bases')
check(U.totalsBasis({ metered: 1.2, estimated: null }) === 'metered $1.20', 'only the known half')
check(U.totalsBasis({ metered: null, estimated: null }) === null, 'neither is no line')

// ── Cost series ──────────────────────────────────────────────────────────────

{
  const H = 3600
  const t0 = H * 1000
  const rows = [
    row(t0, { provider: '' }, { requests: 2, cost_usd: 0.5 }),
    row(t0, { provider: 'openrouter' }, { requests: 1, cost_usd: 0.2 }),
    row(t0 + 2 * H, { provider: null }, { requests: 1, cost_usd: 0.2 }),
  ]
  const local = U.costSeries(rows, 'local', t0, t0 + 3 * H, '1h')
  check(local.length === 3, 'one point per bucket in the window')
  check(eq(local.map((p) => p.v), [0.5, null, 0.2]), 'local rows fold per bucket; a bucket with none is a hole')
  const cloud = U.costSeries(rows, 'cloud', t0, t0 + 3 * H, '1h')
  check(eq(cloud.map((p) => p.v), [0.2, null, null]), 'cloud is its own series')
}

// ── Tables ───────────────────────────────────────────────────────────────────

const bd = (o) => ({ value: 'x', requests: 0, failed: 0, tokensIn: null, tokensOut: null, cost: null,
  cacheRead: null, reasoning: null, unpriced: 0, ...o })

check(eq(U.bySpend([bd({ value: 'a', cost: null, requests: 9 }), bd({ value: 'b', cost: 0.1, requests: 1 }),
  bd({ value: 'c', cost: 2, requests: 1 })]).map((b) => b.cost), [2, 0.1, null]),
'most spend first; unpriced last however busy')

const many = Array.from({ length: 26 }, (_, i) => i)
check(U.capRows(many, false).length === 25, 'a long table is capped')
check(U.capRows(many, true).length === 26, 'and expands to every row')
check(U.capRows([1, 2, 3], false).length === 3, 'a short one is whole')

check(U.share(1, 4) === 0.25, 'a share')
check(U.share(null, 4) === null && U.share(1, 0) === null, 'no share of an unknown or empty total')
check(U.shareTitle(0.42, 0.6) === '42% of spend · 60% of requests', 'share tooltip')
check(U.shareTitle(null, 0.6) === '— of spend · 60% of requests', 'unknown spend share is a dash')
check(U.cachedTitle(250, 1000) === '25% of input cached', 'cached tooltip')
check(U.cachedTitle(null, 1000) === null, 'no tooltip when nothing reported cache reads')
check(U.floorOf(bd({ cost: 0.1, unpriced: 2 })) === true && U.floorOf(bd({ cost: null, unpriced: 2 })) === false,
  'a row is a floor only with priced dollars under it')

const lim = (o) => ({ scope: 'key', scope_id: 'k', limit: 50, effective_limit: 50, used: 0, remaining: 50,
  resets_at: null, near: false, at: false, ...o })
{
  const b = U.budgetOf([lim({ limit_type: 'requests_cycle', fraction: 0.9, limit: 1000, effective_limit: 1000 }),
    lim({ limit_type: 'local_usd_cycle', fraction: 0.4 })])
  check(b?.limit_type === 'local_usd_cycle', 'the budget is a dollar limit, even when a count limit is tighter')
  check(U.budgetText(b) === '40% of $50.00 local_usd_cycle', 'budget text')
}
check(U.budgetText(null) === '—', 'no dollar limit is a dash')

{
  const k = U.kindByModel([
    row(B, { model: 'm1', provider: '' }, { requests: 1 }),
    row(B, { model: 'm1', provider: 'x' }, { requests: 1 }),
    row(B, { model: 'm2', provider: null }, { requests: 1 }),
  ])
  check(k.get('m1') === 'local + cloud', 'a model served both ways')
  check(k.get('m2') === 'local', 'a local-only model, from a null provider too')
}

check(R.money(0.0032) === '$0.0032', 'a cost under a dollar keeps four places')
check(R.money(12.5, true) === '≥ $12.50', 'a floor over a dollar keeps two')

{
  const b = K.breakdown([
    row(B, { key_id: 'k1' }, { requests: 2, unpriced_requests: 1, cache_read_tokens: 100 }),
    row(B + 86400, { key_id: 'k1' }, { requests: 3, unpriced_requests: 2, cache_read_tokens: 200 }),
  ], 'key_id')
  check(b.length === 1 && b[0].unpriced === 3, 'breakdown sums unpriced requests')
  check(b[0].cacheRead === 300, 'and cache reads')
  check(b[0].reasoning === null, 'and leaves reasoning null when no row reported it')
}

{
  const b = K.breakdown([row(B, { key_id: '' }, { requests: 1 }), row(B, { key_id: null }, { requests: 1 })], 'key_id')
  check(b.length === 1 && b[0].value === '—' && b[0].requests === 2,
    'a group the rollups stored as "" is the same absence as null')
}

done()
