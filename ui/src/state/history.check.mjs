// requires: fixtures HISTORY_FIXTURES -- real payloads, captured; it prints the curl lines
// Verifier for the pure history adapters, in the same shape and for the same
// reason as tabs/cluster/layout.check.mjs: there is no test runner in this
// repo (AUDIT-2026-09-06.md:197, "No UI test suite exists (typecheck is the
// only gate)"), and these functions are checkable without a browser.
//
//   node src/state/history.check.mjs
//
// What makes them worth checking at all: ONE route answers with two different
// row shapes. Under six hours it returns raw per-second columns (`power_w`);
// wider, it returns bucket aggregates (`power_w_avg`) and no `memory_total`
// at all. Reading the wrong one does not throw -- it yields an empty chart,
// which is indistinguishable from a machine that was switched off. Typecheck
// cannot catch it either, because every column is optional precisely so both
// shapes fit one type.
//
// The fixtures are REAL payloads captured from a live coordinator's archive
// (spark-4d38 serving, worker-docker gone quiet ~2h before), not hand-written
// objects -- a fixture written from the same reading of the schema that
// produced the bug agrees with the bug.
//
// history.ts is bundled with the esbuild already inside vite rather than
// imported directly, because node's ESM resolver will not resolve its
// extensionless imports.

import { build as bundleWithEsbuild } from 'esbuild'
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const uiRoot = join(here, '..', '..')
// Bundled inside the ui root, not in /tmp: history.ts reaches React through
// state/backend, and a bundle in /tmp has no node_modules above it to resolve
// it from. (layout.check.mjs can use /tmp because layout.ts imports nothing.)
const out = mkdtempSync(join(uiRoot, '.history-check-'))
const bundle = join(out, 'history.mjs')

// The hooks are not under test here -- they are three lines of
// useKeyedResource each -- but they come along with the module, so React comes
// with them. Only the adapters below are exercised.
// esbuild's JS API rather than the launcher under node_modules/.bin:
// that shim is a POSIX script with no .cmd twin, so spawning it by path
// fails on Windows. rows.check.mjs already bundles this way.
await bundleWithEsbuild({
  entryPoints: [join(here, 'history.ts')],
  bundle: true,
  format: 'esm',
  outfile: bundle,
  logLevel: 'warning',
})

const H = await import(pathToFileURL(bundle).href)

const FIXTURES = process.env.HISTORY_FIXTURES
if (!FIXTURES) {
  console.error(
    'Set HISTORY_FIXTURES to a directory holding nodes-raw.json, nodes-1m.json,\n' +
      'requests-raw.json and requests-1m.json, captured from a live coordinator:\n' +
      "  curl -s 'HOST/api/history/nodes?node_id=NODE&from=-5m'  > nodes-raw.json\n" +
      "  curl -s 'HOST/api/history/nodes?node_id=NODE&from=-24h' > nodes-1m.json\n" +
      "  curl -s 'HOST/api/history/requests?from=-6h&limit=500'  > requests-raw.json\n" +
      "  curl -s 'HOST/api/history/requests?from=-7d&limit=500'  > requests-1m.json",
  )
  process.exit(2)
}
const load = (name) => JSON.parse(readFileSync(join(FIXTURES, name), 'utf8'))

let passed = 0
let failed = 0
function ok(cond, what) {
  if (cond) passed++
  else {
    failed++
    console.error(`  FAIL ${what}`)
  }
}

const nodesRaw = load('nodes-raw.json')
const nodes1m = load('nodes-1m.json')
const reqRaw = load('requests-raw.json')
const req1m = load('requests-1m.json')

// ── The fixtures are the shapes we think they are ────────────────────────────

ok(nodesRaw.resolution === 'raw', 'nodes-raw fixture is raw')
ok(nodes1m.resolution === '1m', 'nodes-1m fixture is rolled')
ok(reqRaw.resolution === 'raw', 'requests-raw fixture is raw')
ok(req1m.resolution === '1m', 'requests-1m fixture is rolled')
ok(nodesRaw.samples.length > 0 && nodes1m.samples.length > 0, 'node fixtures are non-empty')

// The duality this file exists for. If the archive ever starts sending
// `memory_total` on a rollup, or drops `power_w` from a raw row, the fallback
// below is silently doing nothing and this is where it shows.
ok('power_w' in nodesRaw.samples[0], 'a raw sample carries power_w')
ok(!('power_w_avg' in nodesRaw.samples[0]), 'a raw sample carries no power_w_avg')
ok('power_w_avg' in nodes1m.samples[0], 'a rolled sample carries power_w_avg')
ok(!('power_w' in nodes1m.samples[0]), 'a rolled sample carries no power_w')
ok('memory_total' in nodesRaw.samples[0], 'a raw sample carries its own memory_total')
ok(!('memory_total' in nodes1m.samples[0]), 'a rolled sample carries NO memory_total')

// ── nodeSeries reads both shapes ─────────────────────────────────────────────

for (const [label, env] of [['raw', nodesRaw], ['1m', nodes1m]]) {
  for (const field of ['power', 'temp', 'util']) {
    const s = H.nodeSeries(env, field)
    ok(s.length === env.samples.length, `${label}/${field}: one point per sample`)
    ok(
      s.some((p) => p.v != null),
      `${label}/${field}: at least one real value (an empty chart is the bug)`,
    )
    ok(
      s.every((p) => Number.isFinite(p.t)),
      `${label}/${field}: every point has a timestamp`,
    )
  }
}

// Memory is the one that needs a denominator, and the rolled shape has none.
{
  const raw = H.nodeSeries(nodesRaw, 'mem')
  ok(
    raw.some((p) => p.v != null && p.v > 0 && p.v <= 100),
    'raw memory is a percentage from the sample’s own memory_total',
  )

  const noTotal = H.nodeSeries(nodes1m, 'mem')
  ok(
    noTotal.every((p) => p.v == null),
    'rolled memory WITHOUT a profile total is null, not zero and not a byte count',
  )

  const total = nodesRaw.samples[0].memory_total
  const withTotal = H.nodeSeries(nodes1m, 'mem', total)
  ok(
    withTotal.some((p) => p.v != null && p.v > 0 && p.v <= 100),
    'rolled memory WITH the profile total is a percentage',
  )
  // The same bytes through both paths must agree, or the chart steps when the
  // window crosses the six-hour boundary.
  const bytes = nodes1m.samples.find((s) => s.memory_used_avg != null)
  if (bytes) {
    const expected = (bytes.memory_used_avg / total) * 100
    const got = withTotal.find((p) => p.t === bytes.ts)
    ok(
      got != null && Math.abs(got.v - expected) < 1e-9,
      'rolled memory uses memory_used_avg over the supplied total',
    )
  }
}

// A missing column yields a null POINT, never a dropped point: a null lifts
// the pen in Chart, so a sampler that went quiet draws as the hole it is.
{
  const holed = {
    ...nodesRaw,
    samples: [
      { node_id: 'n', ts: 1000, power_w: 10 },
      { node_id: 'n', ts: 1001 },
      { node_id: 'n', ts: 1002, power_w: 12 },
    ],
  }
  const s = H.nodeSeries(holed, 'power')
  ok(s.length === 3, 'a sample missing the column is kept as a point')
  ok(s[1].v === null, 'a sample missing the column is null, not zero and not skipped')
}

ok(H.nodeSeries(null, 'power').length === 0, 'no history is an empty series')

// ── depSeries only answers where a rate exists ───────────────────────────────

ok(
  H.depSeries(reqRaw, 'tps').length === 0,
  'raw request rows yield no throughput series (one request is not a rate)',
)
{
  const tps = H.depSeries(req1m, 'tps')
  ok(tps.length > 0, 'rolled buckets yield a throughput series')
  const bucket = req1m.requests.find((r) => r.tokens != null)
  if (bucket) {
    const got = tps.find((p) => p.t === bucket.ts)
    ok(
      got != null && Math.abs(got.v - bucket.tokens / 60) < 1e-9,
      'throughput is tokens divided by the bucket’s own seconds, not by 1',
    )
  }
  const ttft = H.depSeries(req1m, 'ttft')
  ok(
    ttft.some((p) => p.v != null),
    'rolled buckets yield a p50 TTFT series',
  )
}

// ── requestTotals ────────────────────────────────────────────────────────────

ok(H.requestTotals(reqRaw) === null, 'raw rows have no rolled totals to report')
{
  const t = H.requestTotals(req1m)
  ok(t != null, 'rolled rows produce totals')
  const sumN = req1m.requests.reduce((a, r) => a + (r.n ?? 0), 0)
  ok(t.n === sumN, 'counts are summed across buckets')
  const best = req1m.requests
    .filter((r) => r.ttft)
    .sort((a, b) => b.ttft.n - a.ttft.n)[0]
  if (best) {
    ok(
      t.ttft.p99 === best.ttft.p99_ms && t.ttft.n === best.ttft.n,
      'percentiles come from the busiest bucket, never averaged across buckets',
    )
  }
  ok(
    H.requestTotals({ ...req1m, requests: [] }).n === null,
    'a window with no buckets reports null, not a measured zero',
  )
}

// ── The axis note describes the window it actually drew ──────────────────────

ok(H.resolutionNote(nodesRaw, 267).endsWith('samples'), 'raw is described in samples')
ok(H.resolutionNote(nodes1m, 124) === '124 × 1m', 'rolled is described in buckets')
ok(H.resolutionNote({ ...nodes1m, resolution: '1h' }, 5) === '5 × 1h', 'hourly says hourly')
ok(H.bucketSeconds('ring') === null, 'the in-RAM ring is per-sample, not bucketed')
ok(H.bucketSeconds('1h') === 3600, '1h buckets are an hour')

// ── The window table ─────────────────────────────────────────────────────────

ok(H.WINDOW_SPEC['5m'].from === '-5m', 'windows are sent as relative offsets')
ok(
  H.HISTORY_WINDOWS[0] === 'live' && H.HISTORY_WINDOWS.includes('5m'),
  'live is first and 5m is offered (the one window a ring-only node can answer)',
)
ok(
  H.WINDOW_SPEC['24h'].pollMs > H.WINDOW_SPEC['5m'].pollMs,
  'a wider window is polled more slowly, not harder',
)

rmSync(out, { recursive: true, force: true })

console.log('history.check')
console.log(`  ${passed}/${passed + failed} checks passed`)
if (failed > 0) process.exit(1)
