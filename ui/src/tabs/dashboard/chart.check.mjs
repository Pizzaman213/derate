// Verifier for the chart arithmetic. There is no UI test runner in this repo
// (see layout.check.mjs), and chart.ts is pure -- points in, columns and a
// range out -- so it is checkable without a browser:
//
//   node src/tabs/dashboard/chart.check.mjs
//
// What it is guarding, in one sentence each:
//
//   1. A HOLE STAYS A HOLE. The charts are drawn by uPlot now. uPlot happens
//      to default `spanGaps` to false, which is why it was the right library,
//      but a default is a thing that can change in a minor version and the
//      promise it upholds is one this product makes in prose on three
//      different screens. So the promise is pinned here, at the boundary: what
//      `align` hands the renderer has a null wherever the data had one, and
//      never a value carried across.
//
//   2. AN AXIS IS NOT FITTED TO NOISE. The chart this replaced scaled every
//      series to its own min..max, so a memory series between 61% and 63%
//      filled the box and read as an event. A percentage is 0..100 because
//      that is what a percentage means, and a rate starts at zero because the
//      ratio between two moments is what a rate chart is read for.
//
//   3. THE MEMO KEY NOTICES. `seriesKey` is a heuristic -- it is allowed to be
//      -- but the cases it must catch are written down rather than assumed.
//
// chart.ts is bundled with the esbuild inside vite rather than imported
// directly, for the same reason router.check.mjs does it: node's ESM resolver
// will not resolve an extensionless specifier.

import { build as bundleWithEsbuild } from 'esbuild'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const out = mkdtempSync(join(tmpdir(), 'chart-check-'))
const bundle = join(out, 'chart.mjs')

await bundleWithEsbuild({
  entryPoints: [join(here, 'chart.ts')],
  bundle: true,
  format: 'esm',
  outfile: bundle,
  logLevel: 'warning',
})

const { align, bounds, gapAt, gapSpans, kindFor, seriesKey, yRange } = await import(
  pathToFileURL(bundle).href
)

let failures = 0
function check(what, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want)
  if (!ok) {
    failures += 1
    console.error(`FAIL ${what}\n  got  ${JSON.stringify(got)}\n  want ${JSON.stringify(want)}`)
  }
  return ok
}
function checkThat(what, ok) {
  if (!ok) {
    failures += 1
    console.error(`FAIL ${what}`)
  }
  return ok
}

const p = (t, v) => ({ t, v })

// ── 1. A hole stays a hole ───────────────────────────────────────────────────

check(
  'a null value survives alignment as a null',
  align([[p(1, 10), p(2, null), p(3, 30)]]),
  [[1, 2, 3], [10, null, 30]],
)

check(
  'a NaN is a hole, not a number',
  align([[p(1, 10), p(2, NaN), p(3, 30)]])[1],
  [10, null, 30],
)

check(
  'an Infinity is a hole too',
  align([[p(1, Infinity), p(2, 20)]])[1],
  [null, 20],
)

// The band case. The upper edge is a separate series read from a separate
// column of the same rows, so the two can legitimately disagree about which
// timestamps they have -- and where they do, the missing one is a hole rather
// than the other one's value.
check(
  'a timestamp one series lacks becomes a null in that series, never a fill',
  align([
    [p(1, 10), p(2, 20), p(3, 30)],
    [p(1, 15), p(3, 35)],
  ]),
  [
    [1, 2, 3],
    [10, 20, 30],
    [15, null, 35],
  ],
)

checkThat(
  'no column is ever shorter than the x column',
  align([
    [p(1, 1), p(2, 2), p(5, 5)],
    [p(9, 9)],
  ]).every((col, _i, all) => col.length === all[0].length),
)

// ── 2. uPlot's own precondition: a strictly ascending x ──────────────────────
//
// uPlot does not validate this. Handed a descending or repeating x it draws a
// figure rather than an error, which is the worst of the three outcomes.

check(
  'unsorted rows are sorted, not trusted',
  align([[p(3, 30), p(1, 10), p(2, 20)]]),
  [[1, 2, 3], [10, 20, 30]],
)

check(
  'a duplicated timestamp collapses to one column, last write winning',
  align([[p(1, 10), p(1, 11), p(2, 20)]]),
  [[1, 2], [11, 20]],
)

check('an empty series aligns to empty columns', align([[]]), [[], []])

// ── 3. An axis is not fitted to noise ────────────────────────────────────────

check(
  'a percentage is drawn against 0..100 however flat it is',
  yRange('percent', bounds([p(1, 61), p(2, 62), p(3, 63)])),
  { min: 0, max: 100 },
)
check(
  'a percentage at the ceiling is still 0..100',
  yRange('percent', bounds([p(1, 99.5), p(2, 100)])),
  { min: 0, max: 100 },
)
check('a percentage with no samples is still 0..100', yRange('percent', null), { min: 0, max: 100 })

const rate = yRange('rate', bounds([p(1, 90), p(2, 100)]))
check('a rate is anchored at zero', rate.min, 0)
checkThat('a rate leaves headroom above its peak', rate.max > 100)

check(
  'a rate that has only ever been zero still has a height',
  yRange('rate', bounds([p(1, 0), p(2, 0)])),
  { min: 0, max: 1 },
)

// The thermally boring hour. Without a floor this is stretched to the full
// height of the box and read as a thermal event.
const quiet = yRange('span', bounds([p(1, 47.0), p(2, 47.4)]))
checkThat('a narrow span is widened to a floor', quiet.max - quiet.min >= 10)
checkThat(
  'and widened symmetrically, so the data stays centred',
  Math.abs((quiet.min + quiet.max) / 2 - 47.2) < 1e-9,
)

const real = yRange('span', bounds([p(1, 30), p(2, 90)]))
checkThat('a genuinely wide span is not squashed to the floor', real.max - real.min > 60)
checkThat('and is padded off both frames', real.min < 30 && real.max > 90)

for (const kind of ['percent', 'rate', 'span']) {
  for (const b of [null, bounds([p(1, 0)]), bounds([p(1, 5), p(2, 5)])]) {
    const r = yRange(kind, b)
    checkThat(`${kind} never returns a zero-height range`, r.max > r.min)
  }
}

// The units actually passed by the four call sites.
check('percent is bounded', kindFor('%'), 'percent')
check('temperature has no meaningful zero', kindFor('°C'), 'span')
for (const unit of ['W', 'tok/s', 'ms', 'reqs']) {
  check(`${unit} is a rate, anchored at zero`, kindFor(unit), 'rate')
}

// ── 4. Nothing measured is not a measured zero ───────────────────────────────

check('an all-null series has no bounds at all', bounds([p(1, null), p(2, null)]), null)
check('an empty series has no bounds', bounds([]), null)
check(
  'an all-zero series has bounds, and they are zero',
  bounds([p(1, 0), p(2, 0)]),
  { mn: 0, mx: 0, n: 2, last: 0 },
)
check(
  'the count is of real samples, not of points in the window',
  bounds([p(1, 5), p(2, null), p(3, 7)]).n,
  2,
)
check(
  'the latest value is the latest REAL value, not the latest hole',
  bounds([p(1, 5), p(2, null)]).last,
  5,
)

// ── 5. A known hole is not the same flat line as a quiet machine ─────────────

const env = {
  from: 0,
  to: 100,
  resolution: '1m',
  durable: true,
  truncated: false,
  gaps: [
    { node_id: 'a', from_ts: 20, to_ts: 40, reason: 'agent restarted' },
    { node_id: 'a', from_ts: 500, to_ts: 600, reason: 'outside the window' },
    { node_id: 'a', from_ts: -50, to_ts: 10, reason: 'clipped at the near end' },
  ],
}

check(
  'gaps are clipped to what is drawn, and ones outside it are dropped entirely',
  gapSpans(env, 0, 100),
  [
    { from: 0, to: 10, reason: 'clipped at the near end' },
    { from: 20, to: 40, reason: 'agent restarted' },
  ],
)
check('no envelope means no hatching', gapSpans(null, 0, 100), [])
check('a zero-width window draws no hatching', gapSpans(env, 50, 50), [])

const spans = gapSpans(env, 0, 100)
check('an instant inside a hole names the hole', gapAt(spans, 30).reason, 'agent restarted')
check('an instant outside every hole names none', gapAt(spans, 50), null)
check('a hole includes its own edges', gapAt(spans, 20).reason, 'agent restarted')

// ── 6. The memo key notices what it must ─────────────────────────────────────
//
// `seriesKey` is a heuristic and says so in its own comment. These are the
// cases it is not allowed to miss: the live ring appending, the ring trimming
// its far end, and a history refetch replacing the window outright.

const base = [p(1, 10), p(2, 20), p(3, 30)]
checkThat('an appended sample changes the key', seriesKey(base) !== seriesKey([...base, p(4, 40)]))
checkThat('a trimmed head changes the key', seriesKey(base) !== seriesKey(base.slice(1)))
checkThat(
  'the newest value changing changes the key',
  seriesKey(base) !== seriesKey([p(1, 10), p(2, 20), p(3, 31)]),
)
checkThat(
  'the newest value going null changes the key',
  seriesKey(base) !== seriesKey([p(1, 10), p(2, 20), p(3, null)]),
)
checkThat(
  'a wholesale replacement over a different window changes the key',
  seriesKey(base) !== seriesKey([p(101, 10), p(102, 20), p(103, 30)]),
)
checkThat(
  'an identical series keeps its key -- this is the point of the whole thing',
  seriesKey(base) === seriesKey([p(1, 10), p(2, 20), p(3, 30)]),
)
check('an empty series has a key rather than throwing', seriesKey([]), '0')

rmSync(out, { recursive: true, force: true })

if (failures) {
  console.error(`\n${failures} check(s) failed.`)
  process.exit(1)
}
console.log('chart: alignment, axis ranges, gap geometry and memo keys all check out.')
