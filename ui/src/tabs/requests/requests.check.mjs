// The requests screen's pure parts: the "Served by" wording for each kind of
// target, the window a request listing reads, and the request detail page's
// figures and sentences (requestView.ts). Hermetic.

import { load, report } from '../../check/harness.mjs'

const S = await load(import.meta.url, './servedBy.ts')
const V = await load(import.meta.url, './requestView.ts')
const { check, done } = report()

const providers = [{ provider_id: 'ollama', display_name: 'Ollama' }, { provider_id: 'bare' }]
const deployments = [{ deployment_id: 'd1', served_name: 'qwen', node_ids: ['spark-01', 'spark-02'] }]
const row = (p) => ({ target_id: 't', target_kind: 'local', provider_id: '', deployment_id: '', node_id: '', ...p })

const cases = [
  [row({ target_id: '' }), '—', 'no target is a dash'],
  [row({ target_kind: 'remote', provider_id: 'ollama' }), 'Ollama · ollama', 'remote names the provider'],
  [row({ target_kind: 'remote', provider_id: 'bare' }), 'bare · bare', 'remote without a display name'],
  [row({ target_kind: 'remote', provider_id: 'gone' }), 'gone · gone', 'remote provider no longer listed'],
  [row({ deployment_id: 'd1' }), 'qwen on spark-01, spark-02', 'local names the deployment and nodes'],
  [row({ deployment_id: 'dx', node_id: 'spark-03' }), 'spark-03', 'unknown deployment falls back to node'],
  [row({ deployment_id: 'dx', target_id: 'local:qwen' }), 'local:qwen', 'no node falls back to target'],
]
for (const [r, want, why] of cases) {
  const got = S.servedBy(r, providers, deployments)
  check(got === want, `${why}: "${got}" (want "${want}")`)
}

check(S.windowStart('24h') === '-24h', 'windowStart 24h')
check(S.windowStart('7d') === '-7d', 'windowStart 7d')
check(S.windowStart('30d') === '-30d', 'windowStart 30d')
check(S.windowStart(null) === '-24h', 'no window is the last 24h')
check(S.windowStart('all') === '-24h', 'an unknown window is the last 24h')

check(S.cachedShare(300, 1000) === '30% of prompt', 'cachedShare is a share of the prompt')
check(S.cachedShare(0, 1000) === '0% of prompt', 'a reported zero is a measurement, not a hole')
check(S.cachedShare(1, 1000) === '<1% of prompt', 'a nonzero read never rounds to 0%')
check(S.cachedShare(null, 1000) === null, 'unreported cache read has no share')
check(S.cachedShare(5, 0) === null && S.cachedShare(5, null) === null, 'no prompt, no share')

const first = { status: 429, error_message: 'first model quota', retry_after_s: 17 }
const last = { status: 429, error_message: 'second model quota', retry_after_s: 30 }
check(S.rateLimitAttempt({ attempt_chain: [first, { status: 503 }, last] }) === last,
  'last explicit rate-limit attempt wins without confusing a 503 for a 429')
check(S.rateLimitAttempt({ attempt_chain: [{ status: 503 }, { status: 200 }] }) === undefined,
  'an unavailable model is not retrospectively rate limited')
check(S.rateLimitAttempt({ attempt_chain: null }) === undefined,
  'legacy records without a chain have no invented upstream reason')

// ── Request detail (requestView.ts) ─────────────────────────────────────────
// Two real rows from the archive: a served one and one refused at admission.
const full = {
  request_id: 'r-0a864704e40a', ts: 1795406989.28, node_id: 'spark-26af',
  served_name: 'Qwen2.5-0.5B-Instruct-GPTQ-Int4', target_id: 'd-7f8af651', target_kind: 'local',
  provider_id: '', deployment_id: 'd-7f8af651', policy: 'local_first', attempts: 1, retry_reason: '',
  status: 200, error_code: '', error_class: '', prompt_tokens: 30, completion_tokens: 10,
  ttft_ms: 86.92258317023516, decode_ms: 42.866603937000036, duration_ms: 129.90577891469002,
  parked_ms: null, admission_code: '', key_name: 'Connor', finish_reason: 'stop',
  cache_read_tokens: 16, cache_write_tokens: null, cache_error: null, prefix_cache_hit_rate: 0.5333333333333333,
  cost_usd: null, cost_basis: 'unpriced', fallback_used: 0, fallback_reason: null,
}
const refused = {
  request_id: 'r-04c9aba87bc6', ts: 1795407298.8, node_id: 'spark-26af', served_name: 'Qwen3-0.6B',
  target_id: '', target_kind: '', provider_id: '', deployment_id: '', policy: '', attempts: 1,
  retry_reason: '', status: 400, error_code: 'context_length_exceeded', error_class: '',
  prompt_tokens: 22347, completion_tokens: 0, ttft_ms: null, decode_ms: null, duration_ms: null,
  parked_ms: null, admission_code: 'context_length_exceeded', key_name: 'OMP', finish_reason: null,
  cache_read_tokens: null, cache_write_tokens: null, cache_error: 'not_reported', prefix_cache_hit_rate: null,
  cost_usd: null, cost_basis: null, fallback_used: 0, fallback_reason: null,
}
const eq = (got, want, why) => check(JSON.stringify(got) === JSON.stringify(want), `${why}: ${JSON.stringify(got)} (want ${JSON.stringify(want)})`)

eq(V.headline(full, { where: 'near Miami', providerName: null }),
  'Connor, near Miami, got a 200 from Qwen2.5-0.5B-Instruct-GPTQ-Int4 on spark-26af in 130 ms. Served locally on the first try, 53% of the prompt came from cache, and 67% of the time was prefill.',
  'headline of a served request')
eq(V.headline(refused, { where: "on this coordinator's network", providerName: null }),
  "OMP, on this coordinator's network, got a 400 from Qwen3-0.6B on spark-26af. Refused at admission: context_length_exceeded.",
  'headline of a request refused at admission')
eq(V.headline({ ...full, key_name: null, target_kind: 'remote', provider_id: 'oa', attempts: 3 }, { where: null, providerName: 'OpenAI' }),
  'A caller with no key got a 200 from Qwen2.5-0.5B-Instruct-GPTQ-Int4 on spark-26af in 130 ms. Served by OpenAI after 3 attempts, 53% of the prompt came from cache, and 67% of the time was prefill.',
  'headline of a remote, retried, keyless request')

eq(V.fmtMs(129.9), '130 ms', 'fmtMs under a second')
eq(V.fmtMs(1234), '1.23 s', 'fmtMs over a second')
eq(V.fmtMs(12345), '12.3 s', 'fmtMs over ten seconds')
eq(V.fmtMs(null), '—', 'fmtMs of nothing')

eq(V.statusPill({ status: 200, finish_reason: 'stop', error_code: '', error_class: '' }), { text: '200 · stop', tone: 'live' }, 'statusPill ok')
eq(V.statusPill({ status: 429, finish_reason: null, error_code: '', error_class: '' }), { text: '429', tone: 'warn' }, 'statusPill rate limited')
eq(V.statusPill(refused), { text: '400 · context_length_exceeded', tone: 'fault' }, 'statusPill refused')
eq(V.statusPill({ status: null, finish_reason: null, error_code: '', error_class: '' }), { text: 'no status', tone: 'muted' }, 'statusPill no status')

eq(V.clientDevice('Connor’s MacBook Pro (8) (now near the Miami relay)'),
  { device: 'Connor’s MacBook Pro (8)', where: 'near Miami relay', short: 'near Miami' }, 'clientDevice relayed tailnet peer')
eq(V.clientDevice('pi (now in Austin, Texas, United States)'),
  { device: 'pi', where: 'in Austin, Texas, United States', short: 'in Austin, Texas, United States' }, 'clientDevice placed tailnet peer')
eq(V.clientDevice("nas, now on this coordinator's network"),
  { device: 'nas', where: "on this coordinator's network", short: "on this coordinator's network" }, 'clientDevice peer on the LAN')
eq(V.clientDevice('Austin, Texas'), null, 'a database city is not a device')
eq(V.clientDevice(null), null, 'no label is no device')

const chrome = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36'
eq(V.describeUserAgent(chrome), 'Chrome 157 on macOS', 'describeUserAgent Chrome on a Mac')
eq(V.describeUserAgent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36 Edg/120.0'), 'Edge 120 on Windows', 'Edge is not Chrome')
eq(V.describeUserAgent('Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1'), 'Safari 17 on iOS', 'Safari on an iPhone')
eq(V.describeUserAgent('omp/18.16.0'), 'omp/18.16.0', 'a non-browser client is its own UA')
eq(V.describeUserAgent(null), '—', 'no UA')

const ts = V.timeSplit(full)
eq(ts.segments.map((s) => s.key), ['prefill', 'decode'], 'timeSplit segments of a served request')
check(Math.abs(ts.segments[0].ms - 86.9226) < 1e-3 && Math.abs(ts.segments[1].ms - 42.9832) < 1e-3, `timeSplit prefill/decode ${ts.segments.map((s) => s.ms)}`)
check(Math.abs(ts.segments.reduce((a, s) => a + s.ms, 0) - ts.total) < 1e-9, 'timeSplit segments sum to the total')
eq([ts.ticks[0].text, ts.ticks.at(-1).text, ts.ticks.at(-1).at], ['0', '130 ms', 100], 'timeSplit ticks run 0 to the total')
check(ts.ticks.every((t, i) => i === 0 || t.ms > ts.ticks[i - 1].ms), 'timeSplit ticks ascend')
const parked = V.timeSplit({ ...full, parked_ms: 50 })
eq(parked.segments[0], { key: 'queue', label: 'queued', ms: 50 }, 'parking leads the time split')
check(Math.abs(parked.total - 179.9058) < 1e-3, `parking is added to the total: ${parked.total}`)
eq(V.timeSplit({ ...full, ttft_ms: null }).segments.map((s) => s.key), ['total'], 'no first token is one segment')
eq(V.timeSplit(refused), null, 'nothing timed, nothing to draw')

eq(V.tokenSplit(full), { total: 40, segments: [
  { key: 'cached', label: 'prompt, cached', n: 16 }, { key: 'new', label: 'prompt, new', n: 14 }, { key: 'output', label: 'output', n: 10 },
] }, 'tokenSplit with a reported cache')
eq(V.tokenSplit(refused), { total: 22347, segments: [{ key: 'new', label: 'prompt', n: 22347 }] }, 'tokenSplit with no cache and no output')
eq(V.tokenSplit({ prompt_tokens: null, completion_tokens: 0, cache_read_tokens: null }), null, 'no tokens, no bar')

eq(V.cacheHit(full).caption, '16 of 30 read · write —', 'cacheHit caption')
eq(V.cacheHit(refused), { pct: null, caption: 'cache not reported by the server' }, 'cacheHit unreported')

eq(V.costLine(full), { value: 'Unpriced', note: 'No local rate for Qwen2.5-0.5B-Instruct-GPTQ-Int4.', priceFor: 'Qwen2.5-0.5B-Instruct-GPTQ-Int4' }, 'costLine local unpriced')
eq(V.costLine({ ...full, target_kind: 'remote' }).priceFor, null, 'a remote unpriced model is not priced here')
eq(V.costLine(refused).value, 'Not priced', 'costLine of a failed request')
eq(V.costLine({ ...full, cost_usd: 0.0012, cost_basis: 'metered' }), { value: '$0.0012', note: 'metered', priceFor: null }, 'costLine metered')

eq(V.attemptsLine(full), '1, no retries or fallback', 'attemptsLine single')
eq(V.attemptsLine({ attempts: 2, retry_reason: 'timeout', fallback_used: true, fallback_reason: 'cloud' }), '2, retried: timeout, fallback: cloud', 'attemptsLine retried and fell back')

const raw = V.rawFields(refused)
check(raw.length === Object.keys(refused).length, 'rawFields lists every field')
eq(raw.find(([k]) => k === 'ttft_ms'), ['ttft_ms', '—'], 'rawFields renders null as a dash')
eq(V.rawFields({ a: [1, 2] }), [['a', '[1,2]']], 'rawFields renders arrays as JSON')

done()
