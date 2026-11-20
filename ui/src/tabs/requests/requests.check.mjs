// The requests screen's pure parts: the "Served by" wording for each kind of
// target, and the window a request listing reads. Hermetic.

import { load, report } from '../../check/harness.mjs'

const S = await load(import.meta.url, './servedBy.ts')
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

done()
