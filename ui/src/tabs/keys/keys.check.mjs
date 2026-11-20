// Hermetic. Checks keys/rows.ts -- the keys screen's limit, lamp and wording
// rules -- with no coordinator: the thresholds at their edges, the lamp for
// each status, null-vs-0, and the per-field error chip text.
//
//   cd ui && node src/tabs/keys/keys.check.mjs
import { load, report } from '../../check/harness.mjs'

const R = await load(import.meta.url, './rows.ts')
const { check, done } = report()

const NOW = 1_800_000_000
const state = (fraction, o = {}) => ({
  limit_type: 'requests_cycle', scope: 'key', scope_id: 'k1', limit: 10, effective_limit: 10,
  used: fraction * 10, remaining: Math.max(0, 10 - fraction * 10), resets_at: NOW + 3 * 86400,
  fraction, near: fraction >= 0.8 && fraction < 1, at: fraction >= 1, ...o,
})
const key = (o = {}) => ({
  key_id: 'abcdefghij', name: 'k', org_id: 'o1', created_by: 'admin', created_at: NOW - 86400,
  expires_at: NOW + 10 * 86400, status: 'active', disabled_at: null, disabled_by: null,
  disabled_reason: null, last_used_at: null, last_used_ip: null, notes: '', is_admin_key: false,
  admin_username: null, allowed_models: null, routing_policy: null, limits: {},
  cloud_fallback_local_model: null, deleted_at: null, limit_states: [], usage_30d: null, ...o,
})

// ── near / at at the edges ──────────────────────────────────────────────────
const phases = { 0.79: 'ok', 0.8: 'near', 0.99: 'near', 1: 'at', 1.2: 'at' }
for (const [f, want] of Object.entries(phases)) {
  check(R.limitPhase(Number(f)) === want, `fraction ${f} is ${want}`)
}
check(R.limitText(state(0.79), NOW) === null, '0.79 says nothing')
check(R.limitText(state(0.8), NOW) === 'Near limit: requests_cycle 80%', `0.8 -> "${R.limitText(state(0.8), NOW)}"`)
check(R.limitText(state(0.99), NOW) === 'Near limit: requests_cycle 99%', '0.99 floors to 99%, never a rounded-up 100%')
check(R.limitText(state(0.999), NOW) === 'Near limit: requests_cycle 99%', '0.999 still says 99%')
check(R.limitText(state(1), NOW) === 'At limit: requests_cycle, resets in 3d', `1.0 -> "${R.limitText(state(1), NOW)}"`)
check(R.limitText(state(1.2), NOW) === 'At limit: requests_cycle, resets in 3d', '1.2 is still "at"')
check(R.limitText(state(1, { resets_at: null }), NOW) === 'At limit: requests_cycle', 'a cap with no window names no reset')
check(R.limitText(null, NOW) === null, 'no limits, no text')
const lampNear = R.limitLamp(state(0.9))
const lampAt = R.limitLamp(state(1))
check(lampNear.signal === 'warn' && lampNear.hollow, 'near is a hollow warn lamp')
check(lampAt.signal === 'warn' && !lampAt.hollow, 'at is a filled warn lamp')
check(R.limitLamp(state(0.1)).signal === 'live', 'under is live')
check(R.limitLamp(null).signal === 'idle', 'no limit is an idle lamp')

// ── tightest ────────────────────────────────────────────────────────────────
const t = R.tightest([state(0.3, { limit_type: 'rpm' }), state(0.9, { limit_type: 'tpm' }), state(0.5)])
check(t?.limit_type === 'tpm', 'tightest is the max fraction')
check(R.tightest([]) === null && R.tightest(undefined) === null, 'no states -> null')
check(R.tightest([state(0.5, { limit_type: 'a' }), state(0.5, { limit_type: 'b' })]).limit_type === 'a', 'a tie keeps the first')

// ── cycle math (limits/cycle.py) ────────────────────────────────────────────
const D = 86400
const [s0, e0] = R.cycleBounds(1000, 1000 + 5 * D)
check(s0 === 1000 && e0 === 1000 + 30 * D, 'inside the first cycle: anchor .. anchor+30d')
const [s1, e1] = R.cycleBounds(1000, 1000 + 30 * D)
check(s1 === 1000 + 30 * D && e1 === 1000 + 60 * D, 'exactly 30d later is the second cycle')
const [s2] = R.cycleBounds(1000, 1000 + 95 * D)
check(s2 === 1000 + 90 * D, 'day 95 sits in the cycle starting day 90')
check(R.raised(state(0.5, { effective_limit: 20 })) && !R.raised(state(0.5)), 'raised iff effective != configured')

// ── lamp ────────────────────────────────────────────────────────────────────
const lamp = (o) => R.lampFor(key(o))
const eq = (l, signal, hollow) => l.signal === signal && l.hollow === hollow
check(eq(lamp({}), 'live', false), 'active -> live')
check(eq(lamp({ limit_states: [state(0.8)] }), 'warn', true), 'near a limit -> warn hollow')
check(eq(lamp({ limit_states: [state(1)] }), 'warn', false), 'at a limit -> warn')
check(eq(lamp({ limit_states: [state(0.79)] }), 'live', false), '0.79 is still live')
check(eq(lamp({ status: 'disabled' }), 'idle', false), 'disabled -> idle')
check(eq(lamp({ status: 'expired' }), 'fault', false), 'expired -> fault')
check(eq(lamp({ status: 'disabled', limit_states: [state(1)] }), 'idle', false), 'disabled wins over a limit')
check(eq(lamp({ deleted_at: NOW }), 'idle', false), 'deleted -> idle')
check(lamp({}).label.length > 0, 'every lamp has a label')

// ── null vs 0 ───────────────────────────────────────────────────────────────
check(R.count(null) === '—' && R.count(undefined) === '—', 'null count is an em dash')
check(R.count(0) === '0', '0 count is 0')
check(R.money(null) === '—', 'null cost is an em dash')
check(R.money(0) === '$0.0000', '0 cost is a dollar figure')
const none = R.usageCells(null)
check(none.requests === '—' && none.tokens === '—' && none.cost === '—', 'no archive: every usage cell is an em dash')
const zero = R.usageCells({ requests: 0, tokens: 0, cost_usd: null })
check(zero.requests === '0' && zero.tokens === '0' && zero.cost === '—', 'no traffic: 0 requests, 0 tokens, unpriced cost is a dash')
check(R.limitValue('budget_usd_cycle', 2) === '$2.00' && R.limitValue('rpm', 60) === '60', 'limit values carry their unit')

// ── expiry / last used ──────────────────────────────────────────────────────
check(R.expiresText(key({ expires_at: null, is_admin_key: true }), NOW) === 'never', 'admin key: never')
check(R.expiresText(key(), NOW) === 'in 10d', 'future expiry is relative')
check(R.expiresText(key({ expires_at: NOW - 2 * D }), NOW) === 'expired 2d ago', 'past expiry says expired')
check(R.lastUsedText(key(), NOW) === 'never used', 'unused key says never used')
check(R.lastUsedText(key({ last_used_at: NOW - 120, last_used_ip: '10.0.0.5' }), NOW) === '2m ago · 10.0.0.5', 'last used names the address')

// ── error chips ─────────────────────────────────────────────────────────────
check(R.chipText('not_reported', 412) === 'error: not_reported (412 requests)', 'chip text, plural')
check(R.chipText('not_enabled', 1) === 'error: not_enabled (1 request)', 'chip text, singular')
const bucket = (o) => ({ bucket: 0, group: {}, requests: 1, ok: 1, failed: 0, errors: {}, cache_errors: {}, spec_errors: {}, ...o })
const chips = R.fieldChips([
  bucket({ cache_errors: { not_reported: 400 }, spec_errors: { not_enabled: 3 } }),
  bucket({ cache_errors: { not_reported: 12 } }),
])
check(chips.length === 2, 'one chip per field and code')
check(chips[0].text === 'error: not_reported (412 requests)' && chips[0].field === 'cache hit rate', 'cache chips sum across buckets')
check(chips[1].text === 'error: not_enabled (3 requests)' && chips[1].field === 'spec-decode acceptance', 'spec chip')

// ── series: holes stay holes ────────────────────────────────────────────────
const pts = R.series([bucket({ bucket: 3600, requests: 5 }), bucket({ bucket: 3 * 3600, requests: 0 })], (b) => b.requests, 3600, 4 * 3600, '1h')
check(pts.length === 3, 'one point per bucket step')
check(pts[0].v === 5 && pts[1].v === null && pts[2].v === 0, 'a missing bucket is null, a reported 0 stays 0')

// ── breakdown ───────────────────────────────────────────────────────────────
const bd = R.breakdown([
  bucket({ group: { model: 'a' }, requests: 2, tokens_in: 10, tokens_out: null, cost_usd: null }),
  bucket({ group: { model: 'b' }, requests: 5, tokens_in: null, tokens_out: null, cost_usd: 0.5 }),
  bucket({ group: { model: 'a' }, requests: 1, tokens_in: 5, tokens_out: 7, cost_usd: null }),
], 'model')
check(bd[0].value === 'b' && bd[1].value === 'a', 'busiest group first')
check(bd[1].requests === 3 && bd[1].tokensIn === 15 && bd[1].tokensOut === 7 && bd[1].cost === null, 'sums keep null until something reported')
check(bd[0].tokensIn === null, 'a group nothing reported tokens for stays null')

// ── table row ───────────────────────────────────────────────────────────────
const row = R.tableRow(key({ limit_states: [state(0.85, { limit_type: 'rpm' })] }), NOW)
check(row.tightestType === 'rpm' && row.limitText === 'Near limit: rpm 85%', 'table row carries the tightest limit')

done()
