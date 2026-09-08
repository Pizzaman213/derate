// Verifier for the parts of the particle layer that decide WHAT gets drawn.
// There is no test runner in this repo (AUDIT-2026-09-06.md:197: "No UI test
// suite exists (typecheck is the only gate)"), and the emission rule is the
// one thing here worth pinning: a block is a request in flight on a target, so
// a regression that goes back to a metronome must fail something.
//
//   node src/tabs/cluster/particles.check.mjs
//
// Bundled with the esbuild inside vite for the same reason layout.check.mjs
// does it: node's ESM resolver will not take the extensionless specifiers.
// `useParticleField` itself needs a React tree and is not exercised here; its
// two decisions -- which paths to draw on, and how long a flight lasts -- are
// `collapseFlows` and `flightMs`, which are.

import { build as bundleWithEsbuild } from 'esbuild'
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const uiRoot = join(here, '..', '..', '..')
const out = mkdtempSync(join(tmpdir(), 'particles-check-'))
const bundle = join(out, 'particles.mjs')

// esbuild's JS API rather than the launcher under node_modules/.bin:
// that shim is a POSIX script with no .cmd twin, so spawning it by path
// fails on Windows. rows.check.mjs already bundles this way.
await bundleWithEsbuild({
  entryPoints: [join(here, 'particles.ts')],
  bundle: true,
  format: 'esm',
  outfile: bundle,
  logLevel: 'warning',
})

const P = await import(pathToFileURL(bundle).href)

let failures = 0
let checks = 0
function ok(cond, label) {
  checks++
  if (!cond) {
    failures++
    console.error(`  FAIL ${label}`)
  }
}

// ── A DOM small enough to hold a moving rect ─────────────────────────────────
//
// spawnParticle only ever creates a rect, appends it, writes x/y and removes
// it, so this is the whole surface it touches. Frames are driven by hand
// rather than by a clock, which is what makes "the block is 40% along at 40%
// of its span" checkable at all.

let clock = 0
const pending = []
globalThis.performance = { now: () => clock }
globalThis.requestAnimationFrame = (fn) => pending.push(fn)
globalThis.document = {
  createElementNS: () => ({
    attrs: {},
    parent: null,
    setAttribute(k, v) {
      this.attrs[k] = v
    },
    remove() {
      if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this)
      this.removed = true
    },
  }),
}
const newLayer = () => ({
  children: [],
  appendChild(c) {
    c.parent = this
    this.children.push(c)
  },
})
/** Advance to `t` and run every frame callback queued up to that point. */
function tick(t) {
  clock = t
  const due = pending.splice(0, pending.length)
  for (const fn of due) fn(clock)
}

// ── Flight ───────────────────────────────────────────────────────────────────
{
  // A straight 1000-unit path, so position maps to progress with no geometry
  // in the way. The -5/-4 is the rect's own half-size, which centres it.
  const pts = [
    { x: 0, y: 0 },
    { x: 1000, y: 0 },
  ]
  const layer = newLayer()
  let done = 0
  clock = 0
  P.spawnParticle(layer, pts, 'red', 2000, () => done++)
  ok(layer.children.length === 1, 'a flight puts exactly one block on the layer')
  const box = layer.children[0]

  tick(0)
  ok(Number(box.attrs.x) === -5, 'a block starts at the head of its path')
  tick(1000)
  ok(Math.abs(Number(box.attrs.x) - 495) < 0.001, 'half the span puts the block half way along')
  ok(done === 0, 'a block in flight has not reported done')
  tick(1999)
  ok(!box.removed, 'a block survives to the end of its span')
  tick(2000)
  ok(box.removed && layer.children.length === 0, 'a block leaves the layer when its flight ends')
  ok(done === 1, 'onDone fires exactly once, so the in-flight tally can drop')
  tick(3000)
  ok(done === 1, 'onDone does not fire again after the flight')

  // The span is what the measurement sets, so a different duration has to move
  // the block at a different speed over the same geometry.
  const slow = newLayer()
  clock = 0
  P.spawnParticle(slow, pts, 'red', 8000)
  tick(0)
  tick(1000)
  ok(Math.abs(Number(slow.children[0].attrs.x) - 120) < 0.001, 'a longer span walks the same path slower')

  // A degenerate path draws nothing, and must still settle the tally --
  // otherwise the path would be counted as permanently occupied and never
  // draw again.
  const empty = newLayer()
  let settled = 0
  P.spawnParticle(empty, [{ x: 0, y: 0 }], 'red', 1000, () => settled++)
  ok(empty.children.length === 0 && settled === 1, 'a path with no length draws nothing and settles')
}

// ── Flight duration comes off the wire ───────────────────────────────────────
{
  ok(P.flightMs(2.170895) === 2170.895, "a measured mean duration IS the flight's duration")
  ok(P.flightMs(null) === P.FLIGHT_MS, 'a target that has never completed a request falls back')
  ok(P.flightMs(0) === P.FLIGHT_MS, 'a zero duration is nobody measuring, not an instant request')
  ok(P.flightMs(-3) === P.FLIGHT_MS, 'a negative duration is not a duration')
  ok(P.flightMs(Number.NaN) === P.FLIGHT_MS, 'NaN is not a duration')
  ok(P.flightMs(0.04) === P.MIN_FLIGHT_MS, 'a very fast target is clamped to something visible')
  ok(P.flightMs(90) === P.MAX_FLIGHT_MS, 'a very slow target is clamped to something that still moves')
}

// ── Which path a block flies ─────────────────────────────────────────────────
{
  const paths = { 'm#L:d-a': [], 'm#L:d-b': [], 'm#P': [] }
  const flow = (pathKey, inflight, meanDurationS = null) => ({ pathKey, inflight, meanDurationS })

  const idle = P.collapseFlows([flow('m#L:d-a', 0), flow('m#P', 0)], paths)
  ok(idle.size === 0, 'an idle target draws nothing at all')

  const one = P.collapseFlows([flow('m#L:d-a', 3), flow('m#L:d-b', 0)], paths)
  ok(one.size === 1 && one.get('m#L:d-a').inflight === 3, 'blocks land on the busy target, not the idle one')

  // Two remote targets share the one provider rail on the floor, so their
  // counts add up on it rather than one of them winning.
  const shared = P.collapseFlows([flow('m#P', 2, 1), flow('m#P', 1, 4)], paths)
  ok(shared.get('m#P').inflight === 3, 'targets sharing the provider rail sum their in-flight counts')
  ok(shared.get('m#P').meanDurationS === 4, 'a shared rail flies at its slowest contributor')

  // A target whose deployment is not on the floor has no geometry. Dropping it
  // is the point: the alternative is drawing its request on some other
  // machine's path.
  const undrawn = P.collapseFlows([flow('m#L:missing', 5), flow('m#L:d-a', 1)], paths)
  ok(undrawn.size === 1 && undrawn.has('m#L:d-a'), 'a target with no drawn path is dropped, never redirected')

  // Nothing invents a fractional or negative request.
  const odd = P.collapseFlows([flow('m#L:d-a', 0.9), flow('m#L:d-b', -2)], paths)
  ok(odd.size === 0, 'less than one request in flight is no block')

  // The input list is not mutated -- it is a memo of the caller's, reused
  // across ticks.
  const flows = [flow('m#P', 2, 1), flow('m#P', 1, 4)]
  P.collapseFlows(flows, paths)
  ok(flows[0].inflight === 2 && flows[0].meanDurationS === 1, "collapsing does not mutate the caller's flows")
}

// ── Which targets contribute a block at all ──────────────────────────────────
//
// The rule this pins is the one that made the animation invisible: flows are
// built for EVERY served name, not for the selected one. It also pins which of
// the two counters is read, which is a cadence question no type can hold.
{
  const target = (target_id, kind, outstanding, extra = {}) => ({
    target_id,
    kind,
    backend_url: '',
    weight: 1,
    outstanding,
    healthy: true,
    admitting: true,
    strength: 1,
    cost_per_mtok: 0,
    node_ids: [],
    counters: { mean_duration_s: null },
    ...extra,
  })
  const routing = [
    { served_name: 'local-name', targets: [target('d-a', 'local', 4)] },
    { served_name: 'remote-name', targets: [target('openrouter:x', 'remote', 2)] },
  ]

  const all = P.targetFlows(routing, [], false)
  ok(all.length === 2, 'every served name contributes a flow, not just one')
  ok(
    all.some((f) => f.pathKey === 'local-name#L:d-a'),
    'a local target flies its own deployment path',
  )
  ok(
    all.some((f) => f.pathKey === 'remote-name#P'),
    'a remote target flies the drop into the provider box',
  )

  // Nothing here filters by what was drawn -- collapseFlows does that, and the
  // two together are what let a 312-model provider cost one map and no ink.
  const undrawn = P.collapseFlows(all, { 'local-name#L:d-a': [] })
  ok(undrawn.size === 1, 'a flow for a name with no band is dropped downstream, not upstream')

  // The 1 Hz frame wins over the 5s poll for a local target...
  const fresh = P.targetFlows(routing, [{ deployment_id: 'd-a', queue_depth: 9 }], false)
  ok(fresh[0].inflight === 9, 'a live queue_depth beats the slower outstanding count')
  // ...and stops winning the moment it stops arriving.
  const gone = P.targetFlows(routing, [{ deployment_id: 'd-a', queue_depth: 9 }], true)
  ok(gone[0].inflight === 4, 'a stale frame falls back to outstanding rather than freezing')
  // A remote target has no deployment frame to read, so it never takes one.
  const remoteFresh = P.targetFlows(routing, [{ deployment_id: 'openrouter:x', queue_depth: 9 }], false)
  ok(remoteFresh[1].inflight === 2, 'a remote target reads outstanding, never a deployment frame')

  // A coordinator too old to carry counters is not a zero.
  ok(all[0].meanDurationS === null, 'no measured duration is null, never a made-up default')
}

// ── The return leg: a rate becomes a block rate, and is capped ──────────────
//
// The inbound side's bug was a metronome. The outbound side's would be a rate
// that silently rounds to zero on a slow band, or one that spawns a solid bar
// on a fast one -- so both ends of the curve are pinned here.
{
  ok(P.blocksPerSec(0) === 0, 'nothing measured emits nothing')
  ok(P.blocksPerSec(null) === 0 && P.blocksPerSec(undefined) === 0, 'no reading is not a rate')
  ok(P.blocksPerSec(Number.NaN) === 0 && P.blocksPerSec(-5) === 0, 'NaN and a negative are not rates')
  ok(P.blocksPerSec(0.4) === 0, 'a trickle under one token a second draws nothing')
  ok(P.blocksPerSec(1) === P.STREAM_MIN_HZ, 'the slowest measurable rate is the slowest drawn one')
  ok(P.blocksPerSec(3000) === P.STREAM_MAX_HZ, 'a 3000 tok/s deployment does not spawn 3000 blocks')
  ok(P.blocksPerSec(1e9) === P.STREAM_MAX_HZ, 'and neither does anything else')

  const ladder = [1, 10, 100, 1000, 5000].map(P.blocksPerSec)
  ok(ladder.every((v, i) => i === 0 || v >= ladder[i - 1]), 'a faster band never draws slower')
  ok(ladder.every((v) => v <= P.STREAM_MAX_HZ), 'nothing escapes the cap')
  ok(P.STREAM_MAX_HZ * (P.TOPUP_MS / 1000) <= 1, 'the cap is at most one block per path per tick')
}

// ── The emission accumulator ────────────────────────────────────────────────
{
  let c = 0
  let fired = 0
  for (let i = 0; i < 50; i++) {
    const r = P.streamCredit(c, 0, P.TOPUP_MS)
    c = r.credit
    if (r.emit) fired++
  }
  ok(fired === 0 && c === 0, 'an idle band emits nothing and banks nothing')

  c = 0
  fired = 0
  for (let i = 0; i < 10; i++) {
    const r = P.streamCredit(c, 5000, P.TOPUP_MS)
    c = r.credit
    if (r.emit) fired++
  }
  ok(fired === 10, 'a saturated band emits once a tick, and never twice')

  // The whole reason the credit exists: 1 tok/s is 0.5 Hz, which is a tenth of
  // a block per 200ms tick. Rounding that per tick would draw nothing, forever,
  // on a band that is genuinely serving.
  c = 0
  fired = 0
  for (let i = 0; i < 100; i++) {
    const r = P.streamCredit(c, 1, P.TOPUP_MS)
    c = r.credit
    if (r.emit) fired++
  }
  ok(fired === 10, 'a slow band emits at its own rate rather than not at all')

  ok(P.streamCredit(0, 0, 60_000).credit === 0, 'a long idle gap banks no backlog')
  ok(
    P.streamCredit(0.9, 5000, 60_000).credit <= P.STREAM_CREDIT_MAX,
    'credit is capped whatever the gap, so waking does not fire a burst',
  )
}

// ── Streamed, batched, and the honesty of not knowing ───────────────────────
{
  ok(P.streamingRatio([]) === null, 'no rows is not a ratio')
  ok(P.streamingRatio([{}, {}]) === null, 'rows with no streaming column are not a ratio')
  ok(P.streamingRatio([{ streaming: 1 }, { streaming: 0 }]) === 0.5, 'the ratio is over rows that say')
  ok(P.streamingRatio([{ streaming: 1 }, {}]) === 1, 'an absent flag is not a false')

  ok(P.mostlyStreaming(null) === null, 'no ratio is not a verdict')
  ok(P.mostlyStreaming(0.6) === true && P.mostlyStreaming(0.2) === false, 'a majority decides it')

  const tones = [P.streamTone(true), P.streamTone(false), P.streamTone(null)]
  ok(new Set(tones).size === 3, 'streamed, batched and unknown are three different colours')
  ok(!tones.includes('var(--flow)'), 'none of them is the request-in-flight blue')
  ok(P.streamTone(null) === 'var(--ink-muted)', 'unknown paints the no-reading grey, never a default')
  ok(
    P.streamBlockWidth(true) !== P.streamBlockWidth(false),
    'and the difference survives a greyscale screenshot',
  )
}

// ── Which path an out-block flies ───────────────────────────────────────────
{
  const paths = { 'd-a#OUT': [{ x: 0, y: 0 }], 'remote:x#OUT': [{ x: 0, y: 0 }] }
  const s = (pathKey, tokensPerSec) => ({ pathKey, tokensPerSec, streaming: true })
  const kept = P.collapseStreams([s('d-a#OUT', 10), s('nope#OUT', 999)], paths)
  ok(
    kept.length === 1 && kept[0].pathKey === 'd-a#OUT',
    'a band with no drawn return leg is dropped, never redirected',
  )
  ok(P.collapseStreams([s('d-a#OUT', 0)], paths).length === 0, 'a band producing nothing streams nothing')
}

console.log('particles.check')
console.log(`  ${checks - failures}/${checks} checks passed`)
if (failures) process.exit(1)
