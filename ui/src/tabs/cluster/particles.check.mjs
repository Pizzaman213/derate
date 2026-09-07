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

import { execFileSync } from 'node:child_process'
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const uiRoot = join(here, '..', '..', '..')
const out = mkdtempSync(join(tmpdir(), 'particles-check-'))
const bundle = join(out, 'particles.mjs')

execFileSync(
  join(uiRoot, 'node_modules', '.bin', 'esbuild'),
  [join(here, 'particles.ts'), '--bundle', '--format=esm', `--outfile=${bundle}`, '--log-level=warning'],
  { stdio: 'inherit' },
)

const P = await import(bundle)

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

console.log('particles.check')
console.log(`  ${checks - failures}/${checks} checks passed`)
if (failures) process.exit(1)
