// The dashboard Usage card's folds (usageSummary.ts), on literal `/api/usage`
// rows. Hermetic: no coordinator, no browser.
//
// What it pins: a day with no rollup is 0 but a day whose tokens nobody
// reported is null; local/cloud always arrive in that order (it fixes their
// colours); the model mix folds its tail into `other` without losing tokens;
// and `""` and null providers are both local.

import { load, report } from '../../check/harness.mjs'

const S = await load(import.meta.url, './usageSummary.ts')
const { check, done } = report()

const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b)
const DAY = 86400

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

// ── Days ─────────────────────────────────────────────────────────────────────

const today = 20_045 * DAY // a UTC midnight
const to = today + 13 * 3600 + 17
const days7 = S.dayStarts(to, 7)
check(days7.length === 7, 'seven days for a 7d window')
check(days7.every((d) => d % DAY === 0), 'every day start is a UTC midnight')
check(days7.every((d, i) => i === 0 || d - days7[i - 1] === DAY), 'ascending, one day apart')
check(days7[6] === today, 'the last day is the one `to` falls in')
check(S.usageStart(30) === '-29d', '30 days asks for today plus 29 before it')

// ── Tokens per day ───────────────────────────────────────────────────────────

const days3 = S.dayStarts(to, 3)
const perDay = S.tokensPerDay(
  [
    row(days3[0], { model: 'a', provider: '' }, { requests: 2, tokens_in: 10, tokens_out: 5 }),
    row(days3[0], { model: 'b', provider: 'openai' }, { requests: 1, tokens_in: 3, tokens_out: 4 }),
    // days3[1] is absent: nothing served
    row(days3[2], { model: 'a', provider: null }, { requests: 4 }), // served, tokens unreported
  ],
  days3,
)
check(perDay.length === 6, 'one row per day × side')
check(perDay.every((r, i) => r.series === (i % 2 === 0 ? 'local' : 'cloud')), 'series order is always local, cloud')
check(perDay[0].value === 15 && perDay[1].value === 7, 'tokens_in + tokens_out per side')
check(perDay[2].value === 0 && perDay[3].value === 0, 'a day with no rollup is 0 on both sides')
check(perDay[4].value === null, 'a day whose tokens nobody reported is null, not 0')
check(perDay[5].value === 0, 'the side that saw no rows that day is still 0')
check(perDay[0].label === S.dayLabel(days3[0]), 'labelled by UTC day')

// ── Model mix ────────────────────────────────────────────────────────────────

const seven = ['m1', 'm2', 'm3', 'm4', 'm5', 'm6', 'm7'].map((m, i) =>
  row(today, { model: m, provider: '' }, { requests: 1, tokens_out: 700 - i * 100 }),
)
const mix = S.modelShare(seven)
const tokenRows = mix.filter((r) => r.label === 'Tokens')
check(eq(tokenRows.map((r) => r.series), ['m1', 'm2', 'm3', 'm4', 'm5', 'other']), 'top five by tokens, then other')
check(tokenRows.at(-1).value === 200 + 100, 'other is the sum of the two it folded')
check(mix.filter((r) => r.label === 'Requests').at(-1).value === 2, 'other counts their requests too')
const three = S.modelShare(seven.slice(0, 3))
check(!three.some((r) => r.series === 'other'), 'no other row when nothing folded')
check(
  S.modelShare([row(today, { model: '', provider: '' }, { requests: 1, tokens_out: 1 })])[0].series === '—',
  'an empty model name is an em dash',
)

// ── Headline ─────────────────────────────────────────────────────────────────

const unreported = S.headline([row(today, { model: 'a', provider: 'openai' }, { requests: 3 })])
check(unreported.tokens === null && unreported.cloudShare === null, 'no tokens reported: tokens and cloud share are null')
check(unreported.requests === 3 && unreported.models === 1, 'requests and models still count')
const allLocal = S.headline([
  row(today, { model: 'a', provider: '' }, { requests: 24, tokens_in: 0, tokens_out: 1051 }),
  row(today, { model: 'b', provider: null }, { requests: 3, tokens_in: 0, tokens_out: 175 }),
])
check(allLocal.cloudShare === 0, '"" and null providers are both local: cloud share 0')
check(allLocal.tokens === 1226 && allLocal.tokensOut === 1226 && allLocal.tokensIn === 0, 'token totals')
check(allLocal.models === 2, 'two models used')
const mixed = S.headline([
  row(today, { model: 'a', provider: '' }, { requests: 1, tokens_out: 300 }),
  row(today, { model: 'b', provider: 'anthropic' }, { requests: 1, tokens_out: 100 }),
])
check(mixed.cloudShare === 0.25, 'cloud share is cloud tokens over all tokens')
const sides = S.localCloudShare([
  row(today, { model: 'a', provider: '' }, { requests: 1, tokens_out: 300 }),
  row(today, { model: 'b', provider: 'anthropic' }, { requests: 2 }),
])
check(eq(sides.map((r) => [r.label, r.series, r.value]), [
  ['Tokens', 'local', 300], ['Tokens', 'cloud', null],
  ['Requests', 'local', 1], ['Requests', 'cloud', 2],
]), 'local vs cloud: tokens and requests, an unreported side is null')

// ── Health: failures, spend, cache, fallbacks ────────────────────────────────

const health = S.headline([
  row(today, { model: 'a', provider: '' }, { requests: 10, failed: 2, errors: { timeout: 1, upstream_5xx: 1 }, tokens_in: 100, cache_read_tokens: 40, fallback_rate: 0.2, cost_usd: 0.5, unpriced_requests: 0 }),
  row(today, { model: 'b', provider: 'openai' }, { requests: 5, failed: 1, errors: { timeout: 1 }, tokens_in: 100, cache_read_tokens: null, cost_usd: null, unpriced_requests: 5 }),
])
check(health.failed === 3, 'failed sums across rows')
check(health.fallbacks === 2, 'fallbacks are fallback_rate × requests')
check(health.cost === 0.5 && health.costFloor, 'cost sums the priced rows and is a floor beside unpriced requests')
check(health.cachedShare === 0.2, 'cache hits are cache-read prompt tokens over all prompt tokens')
check(S.headline([row(today, { model: 'a', provider: '' }, { requests: 1 })]).cost === null, 'nothing priced: cost is null, not $0')

// ── Failures per day ─────────────────────────────────────────────────────────

const fd = S.dayStarts(to, 3)
const fails = S.failuresPerDay([
  row(fd[0], {}, { errors: { timeout: 1, '': 2 } }),
  row(fd[2], {}, { errors: { timeout: 2 } }),
  ...['a', 'b', 'c', 'd'].map((c) => row(fd[2], {}, { errors: { [c]: 1 } })),
], fd)
const fseries = [...new Set(fails.map((r) => r.series))]
check(fseries[0] === 'timeout' && fseries[1] === 'upstream error status (unclassified)' && fseries.at(-1) === 'other errors' && fseries.length === 5,
  'top four error classes by count (empty class named, an unknown class as itself), then other errors')
const named = [...new Set(S.failuresPerDay([row(fd[0], {}, { errors: { no_target: 1, ReadTimeout: 1 } })], fd).map((r) => r.series))]
check(eq(named.sort(), ['model server stopped answering (ReadTimeout)', 'nothing could serve the model (no_target)']),
  'a known class is named in plain words with its code, so the hover says what failed')
const iso = (d) => new Date(d * 1000).toISOString().slice(0, 10)
const fcell = (d, s) => fails.find((r) => r.x === iso(d) && r.series === s)?.y
check(fcell(fd[0], 'timeout') === 1 && fcell(fd[2], 'timeout') === 2, 'a class on its day')
check(fcell(fd[1], 'timeout') === 0, 'a day with no failures is 0')
check(fcell(fd[2], 'other errors') === 2, 'past the top four (ties by name) the rest fold into other errors')
const none = S.failuresPerDay([row(fd[1], {}, { requests: 5 })], fd)
check(eq(none.map((r) => [r.x, r.series, r.y]), fd.map((d) => [iso(d), 'failed', 0])), 'nothing failed: one zero series on every UTC date, so the chart is a flat line')

// ── Requests per day by key ──────────────────────────────────────────────────

const d2 = S.dayStarts(to, 2)
const byDay = S.requestsPerDayByKey([
  row(d2[0], { key_id: 'k1' }, { requests: 4 }),
  row(d2[1], { key_id: 'k2' }, { requests: 1 }),
  ...Array.from({ length: 5 }, (_, i) => row(d2[1], { key_id: `x${i}` }, { requests: 2 })),
], d2, new Map([['k1', 'chat']]))
const seriesOrder = [...new Set(byDay.map((r) => r.series))]
check(seriesOrder[0] === 'chat' && seriesOrder.at(-1) === 'other keys' && seriesOrder.length === 6, 'top five keys by requests, then other keys')
check(byDay.find((r) => r.label === S.dayLabel(d2[0]) && r.series === 'chat').value === 4, 'a key on its day')
check(byDay.find((r) => r.label === S.dayLabel(d2[0]) && r.series === 'x0').value === 0, 'a key absent on a day is 0')
check(byDay.find((r) => r.label === S.dayLabel(d2[1]) && r.series === 'other keys').value === 2 + 1, 'past the top five (ties by name) the rest fold into other keys')

done()
