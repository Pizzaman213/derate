// Verifier for the pure layout function. There is no test runner in this repo
// (AUDIT-2026-09-06.md:197: "No UI test suite exists (typecheck is the only
// gate)"), and layout.ts is the one piece of the graph whose correctness is
// checkable without a browser -- so it gets checked without one.
//
//   node src/tabs/cluster/layout.check.mjs
//
// layout.ts is bundled with the esbuild that already ships inside vite, rather
// than imported directly, because it has a value import of ../../format and
// node's ESM resolver will not resolve an extensionless specifier.
//
// Also writes layout-preview.svg: the floor at 1, 2, 3, 4, 6, 9, 12 and 16
// machines, stacked, so the density tiers can be eyeballed with no cluster and
// no browser. api/fixtures.ts was deleted in 7626319 and VITE_API_MODE no
// longer exists, so this is the only way to see a shape the live cluster is
// not currently in.

import { build as bundleWithEsbuild } from 'esbuild'
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const uiRoot = join(here, '..', '..', '..')
const out = mkdtempSync(join(tmpdir(), 'layout-check-'))
const bundle = join(out, 'layout.mjs')

// esbuild's JS API rather than the launcher under node_modules/.bin:
// that shim is a POSIX script with no .cmd twin, so spawning it by path
// fails on Windows. rows.check.mjs already bundles this way.
await bundleWithEsbuild({
  entryPoints: [join(here, 'layout.ts')],
  bundle: true,
  format: 'esm',
  outfile: bundle,
  logLevel: 'warning',
})

const L = await import(pathToFileURL(bundle).href)

// ── Fixtures ─────────────────────────────────────────────────────────────────

const id = (i) => `spark-${String(i + 1).padStart(2, '0')}`

function nodes(n) {
  return Array.from({ length: n }, (_, i) => ({
    node_id: id(i),
    hostname: id(i),
    device_class: 'gb10',
    gpu_name: 'NVIDIA GB10',
    state: 'healthy',
    role: i === 0 ? 'coordinator' : 'worker',
    memory_used_pct: 40 + i,
    power_w: 70,
    temp_c: 60,
    util_pct: 50,
    strength: 1,
    deployments: [],
    total_memory: 128 * 1024 ** 3,
  }))
}

/** The wire returns every pair, measured or not. Here the first pair carries a
 *  real figure and nothing else does -- the realistic shape. */
function links(n, measuredPairs = 1) {
  const out = []
  let m = 0
  for (let i = 0; i < n; i++) {
    for (let j = i + 1; j < n; j++) {
      const measured = m < measuredPairs
      if (measured) m++
      out.push(
        measured
          ? { src: id(i), dst: id(j), all_reduce_gbps: 10.2, sendrecv_gbps: 9, latency_us: 40, gpudirect_rdma: false, medium: 'connectx-7', stale: false, measured: true }
          : { src: id(i), dst: id(j), medium: 'ethernet', stale: false, measured: false },
      )
    }
  }
  return out
}

const plan = (tp, pp) => ({
  kind: pp > 1 ? 'pipeline' : 'single',
  tensor_parallel: tp,
  pipeline_parallel: pp,
  expert_parallel: 1,
  data_parallel: 1,
  node_ids: [],
  reason: 'fixture',
  measured_link_gbps: 10.2,
  rejected: [],
})

function deployment(servedName, nodeIds, depId = `d-${servedName}`, state = 'ready', modality) {
  return {
    deployment_id: depId,
    served_name: servedName,
    model_id: `org/${servedName}`,
    runtime: modality === 'speech' ? 'tts' : 'vllm',
    state,
    context_length: 8192,
    max_concurrent_seqs: 8,
    started_at: 0,
    last_error: null,
    node_ids: nodeIds,
    plan: plan(1, nodeIds.length),
    fit: null,
    // Left undefined unless a case is about it, which is also what a
    // coordinator older than the field sends.
    ...(modality ? { modality } : {}),
  }
}

/** One /api/topology remote row: a model a provider serves. `upstream` equal
 *  to `served` is the ordinary case -- an alias is the operator having renamed
 *  it, which is one of the things that puts it on the floor. */
function remote(providerId, served, opts = {}) {
  return {
    target_id: `${providerId}:${opts.upstream ?? served}`,
    provider_id: providerId,
    served_name: served,
    upstream_id: opts.upstream ?? served,
    node_id: opts.nodeId ?? null,
    state: opts.state ?? 'healthy',
    admitting: opts.admitting ?? true,
    tokens_per_sec: opts.tps ?? 0,
    ...(opts.modality ? { modality: opts.modality } : {}),
  }
}

/** A provider serving more names than SHORTLIST_MAX: a catalogue, not a list
 *  anybody chose. */
function catalogue(providerId, count) {
  return Array.from({ length: count }, (_, i) => remote(providerId, `cat-model-${i}`))
}

const NO_SELECTION = { selDep: null, selNode: null, selLink: null }

function build(n, opts = {}) {
  return L.layoutCluster({
    nodes: nodes(n),
    links: links(n, opts.measuredPairs ?? 1),
    deployments: opts.deployments ?? [],
    remotes: opts.remotes ?? [],
    routing: opts.routing ?? [],
    selection: opts.selection ?? NO_SELECTION,
    width: opts.width ?? 1100,
    height: opts.height ?? 640,
    order: opts.order ?? null,
    offsets: opts.offsets ?? {},
  })
}

const cardOf = (l, nodeId) => l.cards.find((c) => c.nodeId === nodeId)

// ── Assertions ───────────────────────────────────────────────────────────────

let failures = 0
let checks = 0
function ok(cond, what) {
  checks++
  if (!cond) {
    failures++
    console.error(`  FAIL  ${what}`)
  }
}

/** The flow connectors are orthogonal M/H/V runs, so parsing them is exact
 *  rather than approximate. */
function segments(d) {
  const out = []
  const tokens = d.match(/[MHV][-\d.\s]+/g) ?? []
  let x = 0, y = 0, started = false
  for (const t of tokens) {
    const cmd = t[0]
    const nums = t.slice(1).trim().split(/\s+/).map(Number)
    if (cmd === 'M') { x = nums[0]; y = nums[1]; started = true; continue }
    if (!started) continue
    const nx = cmd === 'H' ? nums[0] : x
    const ny = cmd === 'V' ? nums[0] : y
    out.push({ x1: x, y1: y, x2: nx, y2: ny })
    x = nx; y = ny
  }
  return out
}

/** Axis-aligned segment against a rect, with a 1-unit tolerance so a run that
 *  merely terminates on a plate's edge does not count as crossing it. */
function segmentHitsRect(s, r) {
  const lo = (a, b) => Math.min(a, b), hi = (a, b) => Math.max(a, b)
  const t = 1
  return (
    hi(s.x1, s.x2) > r.x + t && lo(s.x1, s.x2) < r.x + r.w - t &&
    hi(s.y1, s.y2) > r.y + t && lo(s.y1, s.y2) < r.y + r.h - t
  )
}

const overlaps = (a, b) =>
  a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h

console.log('layout.check')

for (let n = 1; n <= 14; n++) {
  const dep = n > 1 ? [deployment('gpt-oss-120b', [id(0), id(1)])] : [deployment('gpt-oss-120b', [id(0)])]
  const a = build(n, { deployments: dep })
  const b = build(n, { deployments: dep })

  ok(JSON.stringify(a) === JSON.stringify(b), `n=${n} deterministic`)
  ok(a.cards.length === n, `n=${n} draws every node (got ${a.cards.length})`)
  ok(new Set(a.cards.map((c) => c.nodeId)).size === n, `n=${n} draws each node exactly once`)
  ok(a.cards.every((c) => c.w > 0 && c.h > 0), `n=${n} every card has positive size`)
  ok(a.slots.length === n, `n=${n} one slot per node`)

  let collide = false
  for (let i = 0; i < a.cards.length; i++)
    for (let j = i + 1; j < a.cards.length; j++) if (overlaps(a.cards[i], a.cards[j])) collide = true
  ok(!collide, `n=${n} no two cards overlap`)

  // Every measured link reaches the canvas, at every node count.
  const measuredIn = links(n).filter((e) => e.measured)
  const drawn = new Set(a.edges.map((e) => e.linkKey))
  ok(
    measuredIn.every((e) => drawn.has([e.src, e.dst].sort().join('~'))),
    `n=${n} every measured link is drawn`,
  )

  // And no unmeasured link ever carries a figure.
  ok(
    a.edges.filter((e) => !e.measured).every((e) => e.label === 'never measured' && !/\d/.test(e.label)),
    `n=${n} no unmeasured link carries a number`,
  )
  ok(
    a.edges.filter((e) => !e.measured).every((e) => e.width === 1 && e.bracket?.fraction !== undefined ? e.bracket.fraction === 0 : true),
    `n=${n} unmeasured links are a fixed hairline with an empty track`,
  )
  // Nothing may be authored below 9 units, which is 12 rendered -- the floor
  // the rest of the panel keeps. Geometry that carries type is checked here;
  // the sizes themselves live in the renderer.
  ok(a.card.h >= 38 && a.card.w >= 78, `n=${n} plate is at least the chip size`)

  // At four machines and under, the whole mesh is on the canvas.
  if (n <= 4) ok(a.suppressedPairs === 0, `n=${n} whole mesh drawn`)
}

// Tiers flip where they should.
ok(L.tierFor(4) === 'full' && L.tierFor(5) === 'compact', 'tier flips 4 -> 5')
ok(L.tierFor(8) === 'compact' && L.tierFor(9) === 'chip', 'tier flips 8 -> 9')
ok(build(12).kind === 'grid' && build(13).kind === 'ring', 'floor becomes a ring above 12')

// Coordinator first.
ok(build(4).arrangement[0] === id(0), 'coordinator takes the first slot')

// A stored order with a departed id and a node that joined since still places
// everyone exactly once, in the right places.
{
  const stored = ['spark-03', 'gone-01', 'spark-01']
  const l = build(4, { order: stored })
  ok(l.cards.length === 4, 'stale order still places every present node')
  ok(new Set(l.arrangement).size === 4, 'stale order produces no duplicates')
  ok(l.arrangement[0] === 'spark-03' && l.arrangement[1] === 'spark-01', 'stored order is honoured')
  ok(l.arrangement.slice(2).sort().join() === 'spark-02,spark-04', 'nodes that joined later are appended')
  ok(!l.arrangement.includes('gone-01'), 'departed ids are not placed')
}

// The keyboard reorder rewrites the arrangement without losing or duplicating
// anyone. (A pointer drag writes an offset instead -- checked below.)
{
  const before = build(5).arrangement
  const after = L.moveToSlot(before, before[4], 0)
  ok(after[0] === before[4], 'moveToSlot puts the node where it was dropped')
  ok(after.length === before.length && new Set(after).size === after.length, 'moveToSlot loses nobody')
  ok(L.moveToSlot(before, 'nope', 0) === before, 'moveToSlot ignores an unknown id')
}

// ── Hand-placement ──────────────────────────────────────────────────────────
//
// A dragged plate goes exactly where it was dropped, alone, and everything
// drawn OF it follows. This is the whole of what free placement has to be true
// for -- the store round-trips through `order.ts`, but a stored offset that
// the geometry ignores would be a plate that snaps back on reload.

// It moves, by exactly what it was given, and nothing else does.
{
  const base = build(4)
  const moved = build(4, { offsets: { [id(2)]: { x: 40, y: -25 } } })
  const a = cardOf(base, id(2))
  const b = cardOf(moved, id(2))
  ok(b.x === a.x + 40 && b.y === a.y - 25, 'a hand-placed plate lands exactly where it was dropped')
  ok(b.offset.x === 40 && b.offset.y === -25, 'the plate carries the offset it was placed by')
  ok(
    [0, 1, 3].every((i) => {
      const p = cardOf(base, id(i))
      const q = cardOf(moved, id(i))
      return q.x === p.x && q.y === p.y
    }),
    'moving one machine moves no other machine',
  )
  ok(
    moved.cards.filter((c) => c.nodeId !== id(2)).every((c) => c.offset.x === 0 && c.offset.y === 0),
    'a plate nobody moved has no offset',
  )
}

// The slots stay the home grid: they are what `Reset layout` goes back to and
// what the keyboard reorder deals into, so a drag must not drag them along.
{
  const base = build(4)
  const moved = build(4, { offsets: { [id(1)]: { x: 90, y: 60 } } })
  ok(
    base.slots.every((sl, i) => moved.slots[i].x === sl.x && moved.slots[i].y === sl.y),
    'hand-placement does not move the slots',
  )
}

// Garbage in the store is a machine nobody moved, never a floor that fails to
// draw. layout.ts sanitises rather than trusting what localStorage hands back.
{
  const base = build(3)
  const junk = build(3, {
    offsets: {
      [id(0)]: { x: NaN, y: 10 },
      [id(1)]: { x: 1e9, y: 0 },
      'gone-01': { x: 50, y: 50 },
    },
  })
  ok(cardOf(junk, id(0)).offset.x === 0 && cardOf(junk, id(0)).offset.y === 0, 'a NaN offset is no offset')
  ok(Math.abs(cardOf(junk, id(1)).offset.x) <= L.OFFSET_LIMIT, 'an absurd offset is bounded, not obeyed')
  ok(junk.cards.length === base.cards.length, 'an offset for a departed machine places nothing')
}

// Placement is keyed on node_id, which is why the node_id had to stop being
// re-derived from the hostname on every boot (registry/nodeident.py). While it
// was, renaming a machine discarded its place on the floor twice over: the
// sanitiser above drops an offset whose id nobody answers to any more, and the
// renamed machine arrives as a stranger with no offset of its own. With a
// persisted id the arrangement is the operator's, and it survives both a
// rename and the roster changing shape underneath it.
{
  const arranged = [id(0), id(1), id(2)]
  const offsets = { [id(1)]: { x: 40, y: -25 } }
  const before = build(3, { order: arranged, offsets })
  const joined = build(4, { order: arranged, offsets })
  const a = cardOf(before, id(1))
  const b = cardOf(joined, id(1))
  ok(
    b.x === a.x && b.y === a.y && b.offset.x === 40 && b.offset.y === -25,
    'a hand-placed machine keeps its place when another machine joins',
  )
  ok(cardOf(joined, id(3)) !== undefined, 'the machine that joined is still drawn')
  // The other half of the same fact, stated so it cannot regress quietly: a
  // machine whose id changes is a different machine to this floor.
  const renamed = build(3, { order: arranged, offsets: { 'spark-01-renamed': { x: 40, y: -25 } } })
  ok(
    renamed.cards.every((c) => c.offset.x === 0 && c.offset.y === 0),
    'an offset under an id nobody answers to places nothing',
  )
}

// The ink box frames what is drawn, so a plate dragged off the grid has to be
// inside it -- otherwise the fit crops the machine somebody just placed.
{
  const l = build(4, { offsets: { [id(3)]: { x: 220, y: 140 } } })
  const c = cardOf(l, id(3))
  ok(
    c.x >= l.ink.x && c.x + c.w <= l.ink.x + l.ink.w && c.y >= l.ink.y && c.y + c.h <= l.ink.y + l.ink.h,
    'the fit frames a hand-placed plate',
  )
}

// Bands hang below the machines. A plate dragged downwards has to push them,
// or it is drawn on top of the deployment it feeds.
{
  const deployments = [deployment('llama', [id(0), id(1)])]
  const l = build(4, { deployments, offsets: { [id(2)]: { x: 0, y: 300 } } })
  const low = cardOf(l, id(2))
  ok(
    l.bands.every((b) => b.y >= low.y + low.h),
    'a machine dragged down pushes the bands below it',
  )
}

// A link re-aims at the plate's new position: the bracket is the mockup's
// signature element and it can only be drawn where there is a clear channel.
{
  const deployments = [deployment('llama', [id(0), id(1)])]
  const pair = (l) => l.edges.find((e) => e.src === id(0) && e.dst === id(1))
  ok(pair(build(4, { deployments })).kind === 'bracket', 'two plates side by side get a bracket')
  const apart = build(4, { deployments, offsets: { [id(1)]: { x: 0, y: 220 } } })
  ok(pair(apart).kind === 'path', 'dragging one off the row costs the pair its bracket')
  const back = build(4, { deployments, offsets: { [id(1)]: { x: 0, y: 0 } } })
  ok(pair(back).kind === 'bracket', 'a zero offset is the default floor')
}

// ── A link is a squared-off run, and it runs where no machine is ─────────────
//
// Links on a grid floor used to be quadratics. `segments` could not read them,
// so the rule they existed to keep -- a link never passes under a machine it
// is not a link between -- was asserted in a comment and nowhere else. They
// are M/H/V now, which puts them under the same parser and the same proof as
// the flow connectors above.
//
// A ring floor is deliberately exempt: it has no rows to run between and no
// gutters to run in, so a chord from centre to centre stays a chord.
{
  const scenes = [
    ['4 full plates, every pair drawn', build(4)],
    ['4 with the middle plate selected and grown', build(4, { selection: { selDep: null, selNode: id(1), selLink: null } })],
    ['8 compact plates, every pair measured', build(8, { measuredPairs: 999 })],
    ['12 chip plates, every pair measured', build(12, { measuredPairs: 999 })],
    // Two rows apart in one column: the case with no clear run through the row
    // gutter, which has to leave from the side instead.
    ['8 with one plate dropped two rows down its own column', build(8, { measuredPairs: 999, offsets: { [id(0)]: { x: 0, y: 260 } } })],
    ['4 with one plate dragged off level', build(4, { offsets: { [id(2)]: { x: 44, y: 132 } } })],
  ]

  const on = (p, c) => {
    const t = 1.5
    const inside = p.x >= c.x - t && p.x <= c.x + c.w + t && p.y >= c.y - t && p.y <= c.y + c.h + t
    const edge =
      Math.abs(p.x - c.x) <= t || Math.abs(p.x - (c.x + c.w)) <= t ||
      Math.abs(p.y - c.y) <= t || Math.abs(p.y - (c.y + c.h)) <= t
    return inside && edge
  }

  for (const [what, l] of scenes) {
    ok(l.kind === 'grid', `${what}: a grid floor`)
    const paths = l.edges.filter((e) => e.kind === 'path')
    ok(paths.length > 0, `${what}: at least one link is drawn as a path`)

    ok(paths.every((e) => !/[LlQqCcAaSsTt]/.test(e.d)), `${what}: no link curves or slants`)
    ok(
      paths.every((e) => segments(e.d).every((s) => s.x1 === s.x2 || s.y1 === s.y2)),
      `${what}: every link run is axis-aligned`,
    )

    // The rule. Endpoints are excluded because a link is SUPPOSED to touch the
    // two machines it belongs to.
    let crosses = null
    for (const e of paths) {
      const ends = [cardOf(l, e.src), cardOf(l, e.dst)]
      for (const seg of segments(e.d)) {
        for (const card of l.cards) {
          if (ends.includes(card)) continue
          if (segmentHitsRect(seg, card)) crosses ??= `${e.src}-${e.dst} through ${card.nodeId}`
        }
      }
    }
    ok(crosses == null, `${what}: no link crosses a machine it does not touch${crosses ? ` (${crosses})` : ''}`)

    // A link that starts in mid-air reads as a rendering fault. Both ends land
    // on the perimeter of the plate they belong to.
    ok(
      paths.every((e) => {
        const segs = segments(e.d)
        const a = { x: segs[0].x1, y: segs[0].y1 }
        const b = { x: segs[segs.length - 1].x2, y: segs[segs.length - 1].y2 }
        const [p, q] = [cardOf(l, e.src), cardOf(l, e.dst)]
        return (on(a, p) && on(b, q)) || (on(a, q) && on(b, p))
      }),
      `${what}: both ends of every link land on their own plate`,
    )
  }

  // Two squared-off runs on the same line are collinear wherever they overlap,
  // which draws two links as one line and loses the other. It is the failure
  // mode a curve did not have -- two quadratics across the same gutter cross
  // at a point and both stay readable -- and it is the whole reason
  // `assignLanes` exists. Four plates in a row provokes it on its own: the two
  // diagonals of the square both want the middle of the gutter.
  //
  // Both axes, and no exemption for links that share a plate: the risers into
  // the plates are exactly where two links used to be drawn on top of each
  // other, and a reader left with one vertical line and two plates on it
  // cannot tell which of them the crossing run belongs to. `fanOff` is what
  // pulls them apart, and this is the assertion that says it worked.
  for (const [what, l] of scenes) {
    const runs = []
    for (const e of l.edges.filter((e) => e.kind === 'path')) {
      for (const s of segments(e.d)) runs.push({ e, s, flat: s.y1 === s.y2 })
    }
    let shared = null
    for (let i = 0; i < runs.length; i++) {
      for (let j = i + 1; j < runs.length; j++) {
        const a = runs[i], b = runs[j]
        if (a.e === b.e || a.flat !== b.flat) continue
        const [pa, pb] = a.flat ? ['y1', 'x1'] : ['x1', 'y1']
        const qb = a.flat ? 'x2' : 'y2'
        if (a.s[pa] !== b.s[pa]) continue
        const lo = Math.max(Math.min(a.s[pb], a.s[qb]), Math.min(b.s[pb], b.s[qb]))
        const hi = Math.min(Math.max(a.s[pb], a.s[qb]), Math.max(b.s[pb], b.s[qb]))
        if (hi - lo > 2) {
          shared ??= `${a.e.src}-${a.e.dst} and ${b.e.src}-${b.e.dst} on ${a.flat ? 'y' : 'x'}=${a.s[pa]}`
        }
      }
    }
    ok(shared == null, `${what}: no two links share a run${shared ? ` (${shared})` : ''}`)
  }

// ── The filleted wire ────────────────────────────────────────────────────────
//
// `dRender` is a third spelling of a route that already has two, and the only
// one that is ever stroked. The two above it stay the proof: every assertion
// in this file reads `d`, `particles.ts` walks `pts`, and nothing below is
// allowed to loosen either. What is checked here is that the drawn wire is the
// SAME wire -- same ends, never off its own route, and never rounded by more
// than the run it is rounding can pay for.
//
// Rounding is asserted twice on purpose. `roundedPath` is exported and is
// checked directly on polylines whose corners are chosen to sit either side of
// every threshold, because the live floor does not reliably produce a six-unit
// run to prove the clamp with; the scenes then prove the real floor goes
// through it.
{
  const R = L.CORNER_R
  ok(typeof R === 'number' && R > 0, `CORNER_R is a positive number (${R})`)

  const renderPath = (d) => {
    const tokens = d.match(/[MHVQ][-\d.\s]+/g) ?? []
    let x = 0, y = 0
    const pts = []
    const quads = []
    for (const t of tokens) {
      const cmd = t[0]
      const n = t.slice(1).trim().split(/\s+/).map(Number)
      if (cmd === 'M') { x = n[0]; y = n[1]; pts.push({ x, y }); continue }
      if (cmd === 'H') x = n[0]
      else if (cmd === 'V') y = n[0]
      else {
        quads.push({ c: { x: n[0], y: n[1] }, from: { x, y }, to: { x: n[2], y: n[3] } })
        x = n[2]; y = n[3]
      }
      pts.push({ x, y })
    }
    return { pts, quads }
  }

  const route = (d) => {
    const segs = segments(d)
    if (!segs.length) return []
    return [{ x: segs[0].x1, y: segs[0].y1 }, ...segs.map((s) => ({ x: s.x2, y: s.y2 }))]
  }

  /** A point's distance from an axis-aligned polyline. Zero for every point a
   *  fillet emits: the enter and leave both sit ON the runs they cut short. */
  const offRoute = (p, poly) => {
    let best = Infinity
    for (let i = 1; i < poly.length; i++) {
      const a = poly[i - 1], b = poly[i]
      const cx = Math.min(Math.max(p.x, Math.min(a.x, b.x)), Math.max(a.x, b.x))
      const cy = Math.min(Math.max(p.y, Math.min(a.y, b.y)), Math.max(a.y, b.y))
      best = Math.min(best, Math.hypot(p.x - cx, p.y - cy))
    }
    return best
  }

  const same = (a, b) => Math.abs(a.x - b.x) < 0.001 && Math.abs(a.y - b.y) < 0.001

  // ── roundedPath, on polylines built to sit either side of the clamp ────────
  {
    const line = [{ x: 0, y: 0 }, { x: 100, y: 0 }]
    ok(L.roundedPath(line, R) === 'M0 0 H100', 'a straight run is not rounded')
    ok(L.roundedPath([{ x: 5, y: 7 }], R) === '', 'a route of one point draws nothing')

    // Both runs long enough to pay the full radius.
    const elbow = L.roundedPath([{ x: 0, y: 0 }, { x: 100, y: 0 }, { x: 100, y: 100 }], R)
    const e = renderPath(elbow)
    ok(e.quads.length === 1, `one corner is one fillet (${e.quads.length})`)
    ok(same(e.quads[0].c, { x: 100, y: 0 }), 'the fillet bends about the corner itself')
    ok(same(e.quads[0].from, { x: 100 - R, y: 0 }), 'the fillet starts a radius short of the corner')
    ok(same(e.quads[0].to, { x: 100, y: R }), 'and leaves a radius past it')

    // The incoming run is 6, so the corner may only take 3 of it -- a fillet
    // that took the full radius here would start before the run did.
    const tight = renderPath(L.roundedPath([{ x: 0, y: 0 }, { x: 6, y: 0 }, { x: 6, y: 60 }], R))
    ok(tight.quads.length === 1, 'a short run still turns a corner')
    ok(same(tight.quads[0].from, { x: 3, y: 0 }), 'a 6-unit run pays half of itself, not the radius')
    ok(same(tight.quads[0].to, { x: 6, y: 3 }), 'and the far side of the corner matches it')

    // Under a unit there is nothing to draw, so the corner stays mitred rather
    // than gaining a curve worth a rounding error.
    const mitred = renderPath(L.roundedPath([{ x: 0, y: 0 }, { x: 1, y: 0 }, { x: 1, y: 60 }], R))
    ok(mitred.quads.length === 0, 'a corner too short to round is left mitred')

    // A point in line with its neighbours: the connectors are full of them,
    // and a fillet about one draws a curve from a line to the same line.
    const straight = L.roundedPath([{ x: 0, y: 0 }, { x: 40, y: 0 }, { x: 90, y: 0 }], R)
    ok(straight === 'M0 0 H90', `a vertex that is not a corner is not rounded (${straight})`)
    const half = renderPath(
      L.roundedPath([{ x: 0, y: 0 }, { x: 40, y: 0 }, { x: 90, y: 0 }, { x: 90, y: 50 }], R),
    )
    ok(half.quads.length === 1, 'and it does not stop the real corner being rounded')

    // Two corners sharing one run: neither may reach past the middle of it, or
    // the second fillet starts before the first has finished.
    const stairs = renderPath(
      L.roundedPath([{ x: 0, y: 0 }, { x: 0, y: 40 }, { x: 8, y: 40 }, { x: 8, y: 80 }], R),
    )
    ok(stairs.quads.length === 2, 'two corners are two fillets')
    ok(
      stairs.quads[0].to.x <= stairs.quads[1].from.x + 0.001,
      'two fillets on one run do not overlap',
    )
  }

  // ── and on the floor the product actually draws ────────────────────────────
  const drawn = [
    ['4 full plates', build(4)],
    ['8 compact plates, every pair measured', build(8, { measuredPairs: 999 })],
    ['12 chip plates, every pair measured', build(12, { measuredPairs: 999 })],
    ['4 with one plate dragged off level', build(4, { offsets: { [id(2)]: { x: 44, y: 132 } } })],
    ['16 on a ring', build(16, { measuredPairs: 999 })],
  ]

  for (const [what, l] of drawn) {
    const wires = [
      ...l.edges.filter((e) => e.kind === 'path').map((e) => [`${e.src}-${e.dst}`, e.d, e.dRender]),
      ...l.conns.map((c) => [c.id, c.d, c.dRender]),
    ]
    ok(wires.length > 0, `${what}: something is drawn`)

    let ends = null, strays = null, overshot = null
    // A `dRender` that had quietly become a copy of `d` would satisfy every
    // assertion below it -- same ends, no strays, no overshoot -- and draw the
    // hard corners this stage exists to get rid of. So the corners are counted.
    let corners = 0, fillets = 0
    for (const [name, d, dr] of wires) {
      const poly = route(d)
      const drawnPts = renderPath(dr).pts
      // A ring's chord is an `L` and has no runs `segments` can read; its
      // fillet is the line itself, which is what this asserts.
      if (!poly.length) {
        if (dr !== d) ends ??= `${name}: a chord is not rounded`
        continue
      }
      if (!same(drawnPts[0], poly[0]) || !same(drawnPts[drawnPts.length - 1], poly[poly.length - 1])) {
        ends ??= name
      }
      for (const p of drawnPts) if (offRoute(p, poly) > 0.001) strays ??= `${name} at ${p.x},${p.y}`
      corners += Math.max(0, poly.length - 2)
      fillets += renderPath(dr).quads.length
      for (const q of renderPath(dr).quads) {
        const back = Math.hypot(q.from.x - q.c.x, q.from.y - q.c.y)
        const on = Math.hypot(q.to.x - q.c.x, q.to.y - q.c.y)
        if (back > R + 0.001 || on > R + 0.001) overshot ??= `${name}: ${back}/${on} past ${R}`
      }
    }
    // The existing rule is that both ends of every link land on their own
    // plate. It reads `d`, so it only goes on meaning something about the
    // picture while the drawn wire ends where `d` does.
    ok(ends == null, `${what}: the drawn wire ends where its route does${ends ? ` (${ends})` : ''}`)
    ok(strays == null, `${what}: no drawn point leaves its own route${strays ? ` (${strays})` : ''}`)
    ok(overshot == null, `${what}: no fillet is wider than the radius${overshot ? ` (${overshot})` : ''}`)
    ok(corners === 0 || fillets > 0, `${what}: the corners it turns are rounded (${fillets} of ${corners})`)
  }

  // A bracket is not a path and has no wire to round.
  {
    const l = build(2, { measuredPairs: 999 })
    const brackets = l.edges.filter((e) => e.kind === 'bracket')
    ok(brackets.length > 0, 'two facing plates are drawn as a bracket')
    ok(brackets.every((e) => e.dRender === ''), 'a bracket has no filleted wire')
    ok(brackets.every((e) => e.pts.length === 0), 'nor a polyline to move along')
  }

  // `pts` is what `motion.ts` interpolates and what `particles.ts` walks, and
  // it is only those things while it says the same as the path.
  for (const [what, l] of drawn) {
    let apart = null
    for (const e of l.edges.filter((x) => x.kind === 'path')) {
      const segs = segments(e.d)
      if (!segs.length) continue
      const poly = [{ x: segs[0].x1, y: segs[0].y1 }, ...segs.map((sg) => ({ x: sg.x2, y: sg.y2 }))]
      if (poly.length !== e.pts.length) apart ??= `${e.linkKey}: ${poly.length} vs ${e.pts.length}`
      else if (poly.some((q, i) => q.x !== e.pts[i].x || q.y !== e.pts[i].y)) apart ??= e.linkKey
    }
    ok(apart == null, `${what}: the polyline is the path, corner for corner${apart ? ` (${apart})` : ''}`)
  }
}

// ── Crossings ────────────────────────────────────────────────────────────────
//
// The floor draws a complete mesh, and K5 is not planar: past four meshed
// machines the crossings cannot be placed away, only drawn. One of every
// crossing pair is broken so the other reads straight through, and which one
// gives way is bandwidth -- the channel that already means bandwidth -- so no
// new meaning is added to the drawing.
//
// As with the fillet, the hole is punched into `dRender` alone. `d` and `pts`
// are untouched, which is why every assertion above still holds and why a
// packet still flies down a wire that now has a hole in it: the hole is about
// reading the picture, not about where the bytes go.
{
  const R = L.CORNER_R
  const G = L.CROSS_GAP
  ok(typeof G === 'number' && G > 0, `CROSS_GAP is a positive number (${G})`)

  // ── the hole itself ───────────────────────────────────────────────────────
  {
    const line = [{ x: 0, y: 0 }, { x: 100, y: 0 }]
    ok(
      L.roundedPath(line, R, [{ x: 50, y: 0 }]) === `M0 0 H${50 - G / 2} M${50 + G / 2} 0 H100`,
      'a crossing punches a hole centred on itself',
    )
    ok(
      L.roundedPath([{ x: 100, y: 0 }, { x: 0, y: 0 }], R, [{ x: 50, y: 0 }]) ===
        `M100 0 H${50 + G / 2} M${50 - G / 2} 0 H0`,
      'and the same hole whichever way the wire is drawn',
    )
    ok(
      L.roundedPath(line, R, [{ x: 50, y: 9 }]) === 'M0 0 H100',
      'a crossing that is not on the run does not break it',
    )
    ok(
      L.roundedPath(line, R, [{ x: 1, y: 0 }]) === 'M0 0 H100',
      'a crossing too close to the end is left undrawn rather than cutting the wire short',
    )
    const two = L.roundedPath(line, R, [{ x: 70, y: 0 }, { x: 30, y: 0 }])
    ok(
      two === `M0 0 H${30 - G / 2} M${30 + G / 2} 0 H${70 - G / 2} M${70 + G / 2} 0 H100`,
      `two crossings are two holes, in the order the wire is drawn (${two})`,
    )
    // The corner is the one place a hole must never land: a wire that stops
    // short of its own elbow reads as a route that gave up.
    const elbow = L.roundedPath([{ x: 0, y: 0 }, { x: 100, y: 0 }, { x: 100, y: 100 }], R, [
      { x: 100 - R, y: 0 },
    ])
    ok(elbow.match(/M/g).length === 1, 'a crossing inside the fillet does not break the corner')
  }

  // ── and on the floor ──────────────────────────────────────────────────────
  const holesOf = (d) => {
    const tokens = d.match(/[MHVQ][-\d.\s]+/g) ?? []
    let x = 0, y = 0, started = false
    const out = []
    for (const t of tokens) {
      const cmd = t[0]
      const n = t.slice(1).trim().split(/\s+/).map(Number)
      if (cmd === 'M') {
        if (started) out.push({ at: { x: (x + n[0]) / 2, y: (y + n[1]) / 2 }, w: Math.hypot(n[0] - x, n[1] - y) })
        x = n[0]; y = n[1]; started = true
        continue
      }
      if (cmd === 'H') x = n[0]
      else if (cmd === 'V') y = n[0]
      else { x = n[2]; y = n[3] }
    }
    return out
  }

  const near = (a, b) => Math.hypot(a.x - b.x, a.y - b.y) < 0.51

  // The same rule the layout applies, asked of the drawing. `rank` is a proxy:
  // every measured pair in these fixtures carries the same figure, which is
  // asserted rather than assumed, so measured-vs-unmeasured and the key
  // tie-break are the only two cases that can arise here.
  const crossings = (l) => {
    const wires = l.edges
      .filter((e) => e.kind === 'path' && e.d)
      .map((e) => ({ e, segs: segments(e.d) }))
    const out = []
    const t = G / 2 + 0.5
    for (let i = 0; i < wires.length; i++) {
      for (let j = i + 1; j < wires.length; j++) {
        for (const p of wires[i].segs) {
          for (const q of wires[j].segs) {
            const h = p.y1 === p.y2 && q.x1 === q.x2 ? p : q.y1 === q.y2 && p.x1 === p.x2 ? q : null
            if (!h) continue
            const v = h === p ? q : p
            const x = v.x1, y = h.y1
            const inside = (a, lo, hi) => a > Math.min(lo, hi) + t && a < Math.max(lo, hi) - t
            if (!inside(x, h.x1, h.x2) || !inside(y, v.y1, v.y2)) continue
            out.push({ a: wires[i].e, b: wires[j].e, at: { x, y } })
          }
        }
      }
    }
    return out
  }

  const gives = (a, b) =>
    a.measured !== b.measured ? (a.measured ? b : a) : a.linkKey > b.linkKey ? a : b

  const floors = [
    ['4 full plates, one pair measured', build(4)],
    // Past four machines only measured pairs are drawn at all (`showsPair`),
    // so four is the only floor where a measured wire and an unmeasured one
    // can meet -- which is the case the bandwidth rule exists for.
    ['4 with three pairs measured', build(4, { measuredPairs: 3 })],
    ['8 compact plates, every pair measured', build(8, { measuredPairs: 999 })],
    ['12 chip plates, every pair measured', build(12, { measuredPairs: 999 })],
    ['4 with one plate dragged off level', build(4, { offsets: { [id(2)]: { x: 44, y: 132 } } })],
  ]

  let mixed = 0, total = 0
  for (const [what, l] of floors) {
    const widths = new Set(l.edges.filter((e) => e.measured).map((e) => e.width))
    ok(widths.size <= 1, `${what}: every measured pair carries the same figure, so rank is the proxy`)

    const xs = crossings(l)
    total += xs.length
    const holes = new Map(l.edges.filter((e) => e.kind === 'path').map((e) => [e.linkKey, holesOf(e.dRender)]))

    // How close to a corner a crossing may be and still be drawable: the
    // fillet has already eaten `CORNER_R` of the run, and the hole needs its
    // own half plus the margin that keeps it off the fillet. Nearer than that
    // and the layout declines to break the wire, which is the stated
    // behaviour -- so the assertion is about the crossings it CAN draw.
    const reach = R + G / 2 + 0.5
    let unbroken = null, wrongWidth = null, exempt = 0
    for (const c of xs) {
      if (c.a.measured !== c.b.measured) mixed++
      const yields = gives(c.a, c.b)
      const hit = (holes.get(yields.linkKey) ?? []).find((h) => near(h.at, c.at))
      if (hit) {
        if (Math.abs(hit.w - G) > 0.001) wrongWidth ??= `${yields.linkKey} ${hit.w}`
        continue
      }
      // Unbroken is only allowed within a corner's reach on the wire that
      // gave way. Anywhere else it is a crossing drawn as a plus sign.
      const own = segments(yields.d).find(
        (sg) =>
          Math.min(sg.x1, sg.x2) - 0.5 <= c.at.x && c.at.x <= Math.max(sg.x1, sg.x2) + 0.5 &&
          Math.min(sg.y1, sg.y2) - 0.5 <= c.at.y && c.at.y <= Math.max(sg.y1, sg.y2) + 0.5,
      )
      const room = own
        ? Math.min(
            Math.hypot(c.at.x - own.x1, c.at.y - own.y1),
            Math.hypot(c.at.x - own.x2, c.at.y - own.y2),
          )
        : 0
      if (room > reach) unbroken ??= `${yields.linkKey} at ${c.at.x},${c.at.y} (${room} of room)`
      else exempt++
    }
    ok(unbroken == null, `${what}: the slower wire gives way at every crossing it has room to${unbroken ? ` (${unbroken})` : ''}`)
    ok(exempt <= xs.length / 2, `${what}: most crossings have the room (${xs.length - exempt} of ${xs.length})`)
    ok(wrongWidth == null, `${what}: every hole is exactly CROSS_GAP wide${wrongWidth ? ` (${wrongWidth})` : ''}`)

    // The other direction: a hole nothing crosses is a wire drawn with a bite
    // out of it for no reason a reader could ever recover.
    let spurious = null, atCorner = null
    for (const e of l.edges.filter((x) => x.kind === 'path')) {
      const segs = segments(e.d)
      const verts = segs.length ? [{ x: segs[0].x1, y: segs[0].y1 }, ...segs.map((sg) => ({ x: sg.x2, y: sg.y2 }))] : []
      for (const h of holesOf(e.dRender)) {
        if (!xs.some((c) => (c.a === e || c.b === e) && near(c.at, h.at))) {
          spurious ??= `${e.linkKey} at ${h.at.x},${h.at.y}`
        }
        for (const v of verts) {
          if (Math.hypot(v.x - h.at.x, v.y - h.at.y) <= G / 2) atCorner ??= `${e.linkKey} at ${v.x},${v.y}`
        }
      }
    }
    ok(spurious == null, `${what}: no wire is broken where nothing crosses it${spurious ? ` (${spurious})` : ''}`)
    ok(atCorner == null, `${what}: no hole reaches a corner or an attachment${atCorner ? ` (${atCorner})` : ''}`)
  }

  // Neither of the two rules is checked by a floor that never exercises it.
  ok(total > 0, `the fixtures actually cross (${total} crossings)`)
  ok(mixed > 0, `and a measured wire meets an unmeasured one (${mixed} of ${total})`)
}

  // The hop's caption backs onto its own lane, so the lane -- plus the 9 units
  // the caption plate rises above its baseline -- has to fit the headroom the
  // floor reserved for exactly this. Otherwise the label is drawn outside the
  // fitted ink and the top of the drawing is clipped.
  {
    const l = build(4)
    const top = Math.min(...l.cards.map((c) => c.y))
    ok(
      l.edges
        .filter((e) => e.kind === 'path' && e.showLabel)
        .every((e) => e.labelAt.y - 9 >= top - L.ARC_HEADROOM),
      'a hop and its caption stay inside the headroom the floor reserved',
    )
  }
}

// A band's bar spans the machines it occupies, wherever they have been put,
// and "contiguous" is a question about the DRAWING: does the bar run across a
// machine that is not one of this band's own? Three machines on one row, wide
// enough that they are one row, with the band over the outer two.
{
  const deployments = [deployment('llama', [id(0), id(2)])]
  const wideFloor = { deployments, width: 1800 }
  const home = build(3, wideFloor)
  ok(
    home.cards.every((c) => c.row === 0) && !home.bands[0].contiguous,
    'a bar drawn across a machine that is not its own is not contiguous',
  )
  ok(home.bands[0].sublabel.includes(id(0)), 'a non-contiguous band names its machines')

  // Drag the machine in the middle out of the row. Nothing has changed about
  // which columns anybody holds; what changed is what the bar crosses.
  const clear = build(3, { ...wideFloor, offsets: { [id(1)]: { x: 0, y: 260 } } })
  ok(clear.bands[0].contiguous, 'dragging the intruder off the row makes the band contiguous')

  // And the bar follows a member wherever it is put.
  const stretched = build(3, { ...wideFloor, offsets: { [id(2)]: { x: 200, y: 0 } } })
  const b = stretched.bands[0]
  const m = cardOf(stretched, id(2))
  ok(b.x + b.w >= m.x + m.w - 1, "the band's bar follows the machine that was moved")
  ok(
    b.ticks.length === 2 && b.ticks.some((t) => Math.abs(t - (m.x + m.w / 2)) < 1),
    'a tick sits on the moved machine',
  )
}

// The default floor is drawn identically whether or not the caller knows about
// offsets at all. An older caller passes none; the geometry it gets must be
// the geometry it always got.
{
  const deployments = [deployment('llama', [id(0), id(1)]), deployment('qwen', [id(3)])]
  const withKey = build(6, { deployments, offsets: {} })
  const without = L.layoutCluster({
    nodes: nodes(6),
    links: links(6, 1),
    deployments,
    remotes: [],
    routing: [],
    selection: NO_SELECTION,
    width: 1100,
    height: 640,
    order: null,
  })
  ok(
    JSON.stringify(withKey.cards) === JSON.stringify(without.cards) &&
      JSON.stringify(withKey.edges) === JSON.stringify(without.edges) &&
      JSON.stringify(withKey.bands) === JSON.stringify(without.bands),
    'omitting offsets draws the floor it always drew',
  )
}

// Selecting a machine reveals its own unmeasured pairs at a node count where
// the mesh is otherwise suppressed.
{
  const plain = build(8)
  const sel = build(8, { selection: { selDep: null, selNode: id(3), selLink: null } })
  ok(plain.suppressedPairs > sel.suppressedPairs, 'selecting a machine reveals its links')
  ok(
    sel.edges.some((e) => e.src === id(3) || e.dst === id(3)),
    'the selected machine has its links on the canvas',
  )
}

// A deployment relying on an unmeasured link is always drawn, however big the
// cluster gets. That is a warning, not noise.
{
  const l = build(12, { deployments: [deployment('gpt-oss-120b', [id(9), id(10)])] })
  ok(
    l.edges.some((e) => [e.src, e.dst].sort().join('~') === [id(9), id(10)].sort().join('~')),
    'an unmeasured link a deployment spans is always drawn',
  )
}

// Centring. The floor centres the machines in the space left over beside the
// entry box and the bands, which leaves the drawing as a whole sitting left of
// centre; offsetX is what squares that up, and it is the renderer's only job.
{
  const inkOf = (l) => {
    const spans = [
      ...l.cards.map((c) => [c.x, c.x + c.w]),
      ...l.bands.map((b) => [b.x, b.x + b.w]),
      ...(l.entries[0] ? [[l.entries[0].x, l.entries[0].x + l.entries[0].w]] : []),
      ...(l.exits[0] ? [[l.exits[0].x, l.exits[0].x + l.exits[0].w]] : []),
      ...(l.provider ? [[l.provider.x, l.provider.x + l.provider.w]] : []),
    ]
    return [Math.min(...spans.map((v) => v[0])), Math.max(...spans.map((v) => v[1]))]
  }
  for (const n of [1, 2, 3, 4, 6, 9, 12, 16]) {
    const l = build(n, { deployments: [deployment('gpt-oss-120b', [id(0)])], width: 1600 })
    const [a, b] = inkOf(l)
    const left = a + l.offsetX
    const right = l.width - (b + l.offsetX)
    ok(Math.abs(left - right) <= 1, `${n} machines: the drawing is centred (${left} vs ${right})`)
    // A negative offsetX is not a fault at these sizes: it is what centring a
    // drawing WIDER than the viewBox means, and the renderer's fit scales that
    // back in and centres on the ink's own middle either way. What must never
    // happen is a drawing that FITS being pushed off the left edge anyway.
    ok(
      b - a > l.width || l.offsetX >= 0,
      `${n} machines: a drawing that fits starts on the canvas`,
    )
  }
}

// The ink box: what the renderer's fit transform frames. It has to enclose
// every drawn box, or a fitted drawing clips whatever it left out.
{
  for (const n of [1, 2, 4, 9, 16]) {
    const l = build(n, {
      deployments: [deployment('gpt-oss-120b', [id(0)])],
      routing: [{
        served_name: 'gpt-oss-120b', policy: 'local_first', sticky_ttl_s: 0, flow: 'local',
        targets: [
          { target_id: 'd-gpt-oss-120b', kind: 'local', backend_url: '', weight: 0.8, outstanding: 0, healthy: true, admitting: true, strength: 1, cost_per_mtok: 0, node_ids: [id(0)] },
          { target_id: 'openrouter:x', kind: 'remote', backend_url: '', weight: 0.2, outstanding: 0, healthy: true, admitting: true, strength: 1, cost_per_mtok: 0.6 },
        ],
      }],
      width: 1600,
    })
    const boxes = [
      ...l.cards,
      ...l.bands,
      ...(l.entries[0] ? [l.entries[0]] : []),
      ...(l.exits[0] ? [l.exits[0]] : []),
      ...(l.provider ? [l.provider] : []),
    ]
    // Forgetting `boxes.push(exit)` in layout.ts clips the whole return column
    // out of the fit and does it silently, so the fixture has to HAVE one for
    // the enclosure check above to be covering anything.
    ok(l.exits[0] != null, `${n} machines: the exit plate is in this fixture, so the check below covers it`)
    for (const b of boxes) {
      ok(
        b.x >= l.ink.x && b.y >= l.ink.y && b.x + b.w <= l.ink.x + l.ink.w && b.y + b.h <= l.ink.y + l.ink.h,
        `${n} machines: the ink box encloses every drawn box`,
      )
    }
    ok(l.ink.w > 0 && l.ink.h > 0, `${n} machines: the ink box is non-degenerate`)
    ok(l.provider !== null, `${n} machines: the provider bus is in this fixture, so the check above covers it`)
  }
}

// No letterboxing. The SVG fills the floor, so the viewBox aspect must equal
// the element's or preserveAspectRatio pads one axis and the dead space this
// whole arrangement removes comes straight back.
{
  for (const [w, h] of [
    [1600, 900],
    [1100, 640],
    [900, 1200],
    [640, 300],
    [400, 800],
  ]) {
    const l = build(3, { deployments: [deployment('gpt-oss-120b', [id(0)])], width: w, height: h })
    // GW has a 480-unit floor, so compare the RATIO, not the raw figures.
    const want = h / w
    const got = l.height / l.width
    ok(Math.abs(want - got) <= 0.01, `${w}x${h}: the viewBox matches the element's aspect (${got} vs ${want})`)
  }
}

// Bands.
{
  const l = build(4, { deployments: [deployment('gpt-oss-120b', [id(0), id(1)])] })
  ok(l.bands.length === 1, 'a spanning deployment gets a band')
  ok(l.bands[0].contiguous && l.bands[0].leads.length === 2, 'a contiguous band has a lead per machine')
  ok(l.bands[0].plan === 'PP 2', 'the band carries the plan')

  // -- a band that is still arriving ----------------------------------------
  //
  // It is taller, because it carries the launch stepper instead of a
  // throughput readout it has no figure for. Which means band tops can no
  // longer be `bandTop + i * pitch`: the two checks below are the ones that
  // caught it, and the second is the one that matters -- index arithmetic
  // stacks the band after a tall one straight back on top of it.
  {
    const arriving = build(4, {
      deployments: [deployment('gpt-oss-120b', [id(0), id(1)], 'd-a', 'launching')],
    })
    const served = build(4, { deployments: [deployment('gpt-oss-120b', [id(0), id(1)], 'd-a')] })
    ok(arriving.bands[0].loading, 'a launching deployment gets a loading band')
    ok(!served.bands[0].loading, 'and a ready one does not')
    ok(arriving.bands[0].h > served.bands[0].h, 'the loading band is taller than a serving one')
    ok(
      build(4, { deployments: [deployment('m', [id(0)], 'd-p', 'planned')] }).bands[0].loading,
      'planned is arriving too',
    )
    ok(
      !build(4, { deployments: [deployment('m', [id(0)], 'd-d', 'degraded')] }).bands[0].loading,
      'degraded is serving badly, not arriving',
    )
    ok(
      arriving.bands[0].ny === arriving.bands[0].y + arriving.bands[0].h / 2,
      'and its centre -- what the entry and the provider tap meet -- moves with it',
    )

    const stacked = build(4, {
      deployments: [
        deployment('a-model', [id(0)], 'd-a', 'launching'),
        deployment('b-model', [id(1)], 'd-b'),
      ],
    })
    ok(stacked.bands.length === 2, 'two deployments, two bands')
    ok(
      stacked.bands[0].y + stacked.bands[0].h <= stacked.bands[1].y,
      'a band below a loading one is not stacked on top of it',
    )
    ok(
      stacked.bands.every((b) => b.y + b.h <= stacked.ink.y + stacked.ink.h),
      'and the taller band is still inside the ink box',
    )
  }

  // -- the ledger is not the roster -----------------------------------------
  //
  // /api/deployments keeps every attempt. Three failed tries at one model are
  // three rows with one served name on one node, and the band has no
  // vocabulary for "over" -- `degraded` and `loading` are the only conditions
  // it draws -- so each one rendered as an ordinary serving band. The floor
  // showed three identical runners of a model that nothing was running.
  {
    const tries = [
      deployment('phi-3.5', [id(0)], 'd-1', 'failed'),
      deployment('phi-3.5', [id(0)], 'd-2', 'failed'),
      deployment('phi-3.5', [id(0)], 'd-3', 'failed'),
    ]
    const retried = build(4, { deployments: tries })
    ok(retried.bands.length === 0, 'three failed tries at one model draw no band')
    // The plate's caption is composed in ClusterGraph off the same prop, not
    // by the layout, so the shared filter is what keeps the two agreeing: a
    // machine captioned `phi-3.5` under a floor drawing no band for it is the
    // ledger showing through in the one place the layout cannot reach.
    ok(
      L.plateOccupant(L.runners(tries).map((d) => d.served_name)) === 'free',
      'and the machine is not captioned with what failed to start on it',
    )
    ok(
      build(4, { deployments: [deployment('m', [id(0)], 'd-s', 'stopped')] }).bands.length === 0,
      'a stopped deployment holds no machine either',
    )
    ok(
      build(4, {
        deployments: [
          deployment('phi-3.5', [id(0)], 'd-1', 'failed'),
          deployment('phi-3.5', [id(0)], 'd-4', 'ready'),
        ],
      }).bands.length === 1,
      'a retry that worked is one runner, not one plus its history',
    )
    // Not a dedupe. Two live deployments of one name is what the planner
    // recommends when the spare machines would make a better second replica
    // than extra parallelism, routing carries both as targets, and
    // `outFlightKey` is keyed by band precisely so each gets its own stream.
    ok(
      build(4, {
        deployments: [
          deployment('phi-3.5', [id(0)], 'd-1'),
          deployment('phi-3.5', [id(1)], 'd-2'),
        ],
      }).bands.length === 2,
      'but two live replicas of one name stay two bands',
    )
  }

  const solo = build(4, { deployments: [deployment('gpt-oss-120b', [id(0)])] })
  ok(solo.bands.length === 1, 'a single-node deployment gets a band too')
  ok(solo.bands[0].w === solo.card.w, 'a solo band whose words fit spans exactly its one machine')
  ok(solo.entries[0] != null, 'a solo deployment still gets an entry box')
  ok(solo.conns.some((c) => c.id.startsWith('entry-')), 'the entry box connects to the solo band')

  // -- the band label never runs under its own throughput readout ------------
  //
  // The readout is right-anchored inside the band and the label was clipped to
  // the band's FULL width, so `Qwen2.5-0.5B-Instruct` on a one-machine plate
  // ran underneath the number and neither could be read. The gutter below is
  // what the band's WIDTH is measured from now (layout `bandNeed`), so the
  // label fits inside it and the clip is a backstop; these pin the backstop's
  // own shape -- where it cuts if a browser's font is not the one we measured,
  // and that a narrow band does not trade the whole name away for a number.
  const GUTTER = 6 * 7.8 + 11
  for (const w of [L.CARD.full.w, L.CARD.compact.w, L.CARD.chip.w, 200, 400]) {
    const label = L.bandLabelWidth(w)
    ok(label < w, `a ${w}-wide band leaves its readout a gutter`)
    ok(label >= w / 2, `a ${w}-wide band keeps at least half its width for the name`)
    ok(w - label <= GUTTER + 0.001, `a ${w}-wide band never gives up more than the gutter`)
  }
  // Wide enough and the gutter is the whole cost: the name is not squeezed
  // further just because there is room to squeeze it.
  ok(
    Math.abs(L.bandLabelWidth(400) - (400 - GUTTER)) < 0.001,
    'a wide band gives up exactly the gutter and no more',
  )
  // The case that started it: 21 characters of 10px mono is 126 units, and the
  // 11-unit inset puts its end at 137 -- past a full-tier plate's 138 minus the
  // readout. So the plate's width cannot be the band's width, and the band it
  // actually gets is wide enough to say the whole thing.
  ok(
    11 + 'Qwen2.5-0.5B-Instruct'.length * 6 > L.bandLabelWidth(L.CARD.full.w),
    'the name that surfaced this does not fit a band the size of one plate',
  )
  {
    const live = build(2, { deployments: [deployment('Qwen2.5-0.5B-Instruct', [id(0)])] })
    const band = live.bands[0]
    ok(band.w > live.card.w, 'so its band grows past the machine it is served by')
    ok(
      11 + 'Qwen2.5-0.5B-Instruct'.length * 6 <= L.bandLabelWidth(band.w),
      'and the whole name is drawn, clear of the readout',
    )
    ok(band.leads.length === 1, 'the lead under it still says which machine is its own')
  }
  ok(L.bandLabelWidth(0) === 0, 'a zero-width band asks for no label width')

  // Dragged apart: members no longer adjacent.
  const split = build(4, {
    deployments: [deployment('gpt-oss-120b', [id(0), id(2)])],
    order: [id(0), id(1), id(2), id(3)],
  })
  ok(!split.bands[0].contiguous, 'a non-adjacent band is drawn open')
  ok(split.bands[0].ticks.length === 2, 'an open band ticks each member')
}

// Particle paths line up with routing targets, one for one.
{
  const routing = [
    {
      served_name: 'gpt-oss-120b',
      policy: 'weighted_capacity',
      sticky_ttl_s: 0,
      flow: null,
      targets: [
        { target_id: 'd-b', kind: 'local', backend_url: '', weight: 0.7, outstanding: 0, healthy: true, admitting: true, strength: 1, cost_per_mtok: 0, node_ids: [id(2)] },
        { target_id: 'd-a', kind: 'local', backend_url: '', weight: 0.3, outstanding: 0, healthy: true, admitting: true, strength: 1, cost_per_mtok: 0, node_ids: [id(0), id(1)] },
      ],
    },
  ]
  const l = build(4, {
    routing,
    deployments: [
      deployment('gpt-oss-120b', [id(0), id(1)], 'd-a'),
      deployment('gpt-oss-120b', [id(2)], 'd-b'),
    ],
  })
  ok(l.paths['gpt-oss-120b#L:d-a'] != null && l.paths['gpt-oss-120b#L:d-b'] != null, 'a path per routing target')
  // Keyed by target id, so a path is bound to its target no matter what order
  // cfg.targets lists them in or which deployments happen to be drawable:
  // d-b is the SOLO one -- shorter, because it has no inter-machine hop, but
  // it still starts at the endpoint.
  ok(l.paths['gpt-oss-120b#L:d-b'].length === 6, "a solo target's path is keyed to that target")
  ok(l.paths['gpt-oss-120b#L:d-a'].length > 6, 'the two-machine target walks the whole pipeline')
  ok(
    l.paths['gpt-oss-120b#L:d-b'][0].x === l.entries[0].x + l.entries[0].w,
    'a flight starts at the entry box',
  )
  // Derived, never a literal: the plate grows leftward as its label grows, so
  // a hardcoded x silently stops meaning "the entry box" the day it changes.
  ok(l.entries[0].x + l.entries[0].w === L.ENTRY_R, 'and the entry box still ends where the gutters are measured from')
  // A path keyed by position would follow whatever else got drawn; keyed by id
  // it cannot, so the ordinal keys must be gone entirely.
  ok(l.paths['gpt-oss-120b#L0'] == null, 'no ordinal flight keys survive')
  // The flight has to end on the last stage, not wherever the geometry happened
  // to point.
  const pipeline = l.paths['gpt-oss-120b#L:d-a']
  const last = pipeline[pipeline.length - 1]
  const target = l.cards.find((c) => c.nodeId === id(1))
  ok(
    Math.abs(last.x - (target.x + target.w / 2)) < 0.001 && Math.abs(last.y - (target.y + target.h / 2)) < 0.001,
    'a pipeline flight ends on its last stage',
  )
}

// No flow connector may pass through a machine plate. The entry fan-out and
// the provider rail both live in gutters left of the floor precisely so this
// holds -- a connector aimed at a plate in column two would have to cross
// column one.
{
  const routing = [{
    served_name: 'gpt-oss-120b', policy: 'local_first', sticky_ttl_s: 0, flow: 'local',
    targets: [
      { target_id: 'd-gpt-oss-120b', kind: 'local', backend_url: '', weight: 0.8, outstanding: 0, healthy: true, admitting: true, strength: 1, cost_per_mtok: 0, node_ids: [id(0), id(1)] },
      { target_id: 'openrouter:x', kind: 'remote', backend_url: '', weight: 0.2, outstanding: 0, healthy: true, admitting: true, strength: 1, cost_per_mtok: 0.6 },
    ],
  }]
  for (const n of [2, 4, 6, 9, 12]) {
    const l = build(n, { routing, deployments: [deployment('gpt-oss-120b', [id(0), id(1)])] })
    let crosses = false
    for (const c of l.conns) {
      for (const seg of segments(c.d)) {
        for (const card of l.cards) if (segmentHitsRect(seg, card)) crosses = true
      }
    }
    ok(!crosses, `n=${n} no flow connector passes through a plate`)
  }

  // The bus exists if and only if some routing config has a remote target.
  const withRemote = build(4, { routing, deployments: [deployment('gpt-oss-120b', [id(0), id(1)])] })
  ok(withRemote.provider != null, 'a remote target draws the provider bus')
  // Tombstones. A peer re-adding either would otherwise pass tsc silently.
  ok(!('boundaryY' in withRemote), 'boundaryY is gone from the layout, not merely unread')
  ok(
    withRemote.conns.every((c) => !('dashed' in c)),
    'the dash vocabulary is gone from the connectors, not left with no user',
  )
  ok(withRemote.junctions.length === 1, 'a configured remote target gets a junction dot')
  ok(withRemote.paths['gpt-oss-120b#P'] != null, 'a remote target gets a provider flight path')

  // The drop, and the two things about it that were wrong before it was one.
  {
    const band = withRemote.bands.find((b) => b.servedName === 'gpt-oss-120b')
    const pts = withRemote.paths['gpt-oss-120b#P']
    const drop = withRemote.conns.find((c) => c.id === `prov-${band.id}`)
    ok(drop != null, 'an allowed band gets its own drop into the bus')

    // It leaves the band's far corner, NOT its centreline: the entry connector
    // arrives at `ny` and the return leg leaves from it, and a tap drawn there
    // is drawn underneath both of them.
    ok(
      withRemote.junctions[0].y === band.y + band.h && withRemote.junctions[0].y !== band.ny,
      'the drop leaves the band below the line the entry and the exit already use',
    )

    // The block crosses the name it is served by. The ported path turned down
    // the gutter short of the band, so this never once held.
    ok(
      pts.some((pt) => pt.y === band.ny && pt.x >= band.x && pt.x <= band.x + band.w),
      'a provider flight crosses the band it is being served for',
    )

    // And it stops where the ink stops. These used to disagree -- the rail
    // ended at the box's left edge and the block carried on to its centre --
    // which is a block sliding out from under a line and over the label.
    const last = pts[pts.length - 1]
    ok(
      last.x === withRemote.provider.x + withRemote.provider.w && last.y === withRemote.provider.y + 19,
      'a provider flight lands exactly where its line lands, on the box',
    )

    // The corridor is past the floor and short of the return column, so it
    // crosses no band on its way down and no plate on its way past.
    const floorRight = Math.max(...withRemote.bands.map((b) => b.x + b.w))
    const dropX = Math.max(...pts.map((pt) => pt.x))
    ok(
      dropX > floorRight && dropX < withRemote.exits[0].x,
      'the drop runs between the floor and the return column',
    )
  }

  // ── The bus answers ───────────────────────────────────────────────────────
  //
  // It used to be a dead end -- everything on the canvas dropped into it and
  // nothing ever left -- so the one box drawn for somebody else's hardware was
  // also the only box with no response side. These are the four things that
  // makes true, and the fifth that it must NOT make true.
  {
    const l = withRemote
    const out = l.conns.filter((c) => c.id.startsWith('provider-out-'))
    ok(out.length === 1, 'the bus has an outbound run: it is not a dead end')

    const segs = segments(out[0].d)
    const start = { x: segs[0].x1, y: segs[0].y1 }
    const end = segs[segs.length - 1]
    const busY = l.provider.y + 19

    // Same edge as the drop lands on, and NOT the same point: a wire that
    // arrives and departs at one dot reads as one wire, which is the reading
    // the band's own drop was moved 18 units clear of its return leg to avoid.
    ok(
      start.x === l.provider.x + l.provider.w && start.y === busY + L.PROVIDER_OUT_DY,
      'the answer leaves the bus by the edge the request landed on, clear of it',
    )
    ok(
      start.y > l.provider.y && start.y < l.provider.y + l.provider.h,
      'and leaves from inside the box, at any row count',
    )

    // It lands where every other response lands. Same plate, same point --
    // the response column is one column, not one per kind of hardware.
    ok(
      end.x2 === l.exits[0].x && end.y2 === l.exits[0].y + l.exits[0].h / 2,
      'and lands on the exit plate, exactly where a band return lands',
    )

    // Up the same rail the bands use, so it is the return column and not a
    // second one drawn beside it.
    const bandExit = segments(l.conns.find((c) => c.id.startsWith('exit-')).d)
    const railX = Math.max(...segs.map((sg) => Math.min(sg.x1, sg.x2)).filter((x) => x < l.exits[0].x))
    ok(
      railX === Math.max(...bandExit.map((sg) => Math.min(sg.x1, sg.x2)).filter((x) => x < l.exits[0].x)),
      'by the rail the bands return on, not a column of its own',
    )

    // THE DOUBLE-DRAW GUARD. Those tokens are already measured and already
    // animated, leaving the band by its own tok/s readout. A flight keyed to
    // this run would draw one measurement in two places and let a reader count
    // the same tokens twice.
    ok(
      Object.values(l.paths).every((pts) => pts[0].x !== start.x || pts[0].y !== start.y),
      'and no flight is keyed to it: the blocks stay where the number is',
    )
  }

  // No bus, no run out of it.
  ok(
    build(4, {
      routing: [{ ...routing[0], targets: [routing[0].targets[0]] }],
      deployments: [deployment('gpt-oss-120b', [id(0), id(1)])],
    }).conns.every((c) => !c.id.startsWith('provider-out-')),
    'a floor with no bus has nothing leaving one',
  )

  // The plate it answers to is the plate that ASKED. A provider serving
  // nothing but speech must not draw its audio back to a chat exit -- the
  // same rule the bands' own return legs are grouped by.
  {
    const speech = build(4, {
      remotes: [remote('openai', 'tts-1', { modality: 'speech' })],
      routing: [],
    })
    const out = speech.conns.filter((c) => c.id.startsWith('provider-out-'))
    ok(
      out.length === 1 && out[0].id === 'provider-out-speech',
      'a bus that answers only for speech returns to the speech plate alone',
    )
    const end = segments(out[0].d).slice(-1)[0]
    const plate = speech.exits.find((e) => e.modality === 'speech')
    ok(
      end.y2 === plate.y + plate.h / 2,
      'and lands on it, not on whichever plate happened to be first',
    )
  }

  // A name no provider serves is connected to nothing. Every band used to get
  // a hairline meaning "reachable through the proxy"; the allowlist made that
  // a claim about routing that cannot happen.
  {
    const localOnlyBand = build(4, {
      deployments: [deployment('gpt-oss-120b', [id(0), id(1)]), deployment('mine-alone', [id(2)], 'd-mine')],
      routing,
      remotes: [remote('openrouter', 'gpt-oss-120b')],
    })
    const alone = localOnlyBand.bands.find((b) => b.servedName === 'mine-alone')
    ok(alone != null, 'the fixture has a name no provider offers')
    ok(
      !localOnlyBand.conns.some((c) => c.id === `prov-${alone.id}`),
      'a name no provider serves gets no connection to the bus',
    )
    ok(
      localOnlyBand.paths['mine-alone#P'] == null,
      'and no provider flight path either',
    )
  }

  // ── The return side ───────────────────────────────────────────────────────
  {
    const l = build(4, { routing, deployments: [deployment('gpt-oss-120b', [id(0), id(1)])] })
    ok(l.exits[0] != null, 'a drawing with bands has an exit plate')
    ok(build(4).exits[0] == null, 'no bands means no exit plate, exactly as it means no entry box')
    ok(
      l.exits[0].y + l.exits[0].h / 2 === l.entries[0].y + l.entries[0].h / 2,
      'the entry and the exit sit on the same line',
    )
    ok(/text\/event-stream/.test(L.EXIT_LABEL), 'the exit plate names the streamed response')
    ok(
      L.EXIT_W >= 2 * L.EXIT_PAD + L.EXIT_LABEL.length * L.EXIT_FONT * 0.6,
      'the exit label fits inside its own plate',
    )
    ok(/\/v1\/chat\/completions$/.test(L.ENTRY_LABEL), 'the entry plate names the whole route, never a truncation')
    ok(
      L.ENTRY_W >= 2 * L.ENTRY_PAD + L.ENTRY_LABEL.length * L.ENTRY_FONT * 0.6,
      'the endpoint label fits inside its own plate',
    )
    // The entry column, left to right. The provider rail used to be the third
    // of these; it now drops on the far side of the floor, so this holds the
    // two that remain rather than pretending it still sits here.
    ok(
      L.ENTRY_R <= L.GUTTER_ENTRY && L.GUTTER_ENTRY < L.FLOOR_X,
      'the left-hand columns stay in order',
    )
    ok(
      L.GUTTER_PROV < L.GUTTER_EXIT,
      'the provider drop is inside the return column, not past it',
    )

    // The return column has to clear everything the floor draws, or a leg is
    // drawn straight through a machine.
    ok(
      l.exits[0].x >= Math.max(...l.cards.map((c) => c.x + c.w)),
      'the exit plate is clear of every machine',
    )
    ok(
      l.exits[0].x >= Math.max(...l.bands.map((b) => b.x + b.w)),
      'the exit plate is clear of every band',
    )
    ok(
      l.provider == null || l.exits[0].x >= l.provider.x + l.provider.w,
      'the exit plate is clear of the provider bus',
    )

    const outs = Object.keys(l.paths).filter((k) => k.endsWith('#OUT'))
    ok(outs.length === l.bands.length, 'every band streams back out')
    for (const key of outs) {
      const pts = l.paths[key]
      const band = l.bands.find((b) => L.outFlightKey(b.id) === key)
      ok(
        pts[0].x === band.x + band.w && pts[0].y === band.ny,
        'an out-stream leaves the band by its own tok/s readout',
      )
      const end = pts[pts.length - 1]
      ok(
        end.x === l.exits[0].x && end.y === l.exits[0].y + l.exits[0].h / 2,
        'and lands on the exit plate',
      )
    }
    // #OUT is keyed by band id; #L:/#P are keyed by served name. They must not
    // be able to collide.
    ok(
      new Set(Object.keys(l.paths)).size === Object.keys(l.paths).length,
      'no flight key collides with an out-stream key',
    )
    // The inbound paths are NOT extended to reach the exit: a request block
    // still means a request in flight and still ends where the work happens.
    for (const key of Object.keys(l.paths).filter((k) => !k.endsWith('#OUT'))) {
      ok(
        l.paths[key].every((pt) => pt.x < l.exits[0].x),
        'an inbound flight still ends at its work, never at the exit plate',
      )
    }
  }

  // ── One plate per endpoint family ─────────────────────────────────────────
  //
  // The floor can carry a speech deployment and a chat one at the same time,
  // and they do not arrive at the same URL: a chat request naming a TTS model
  // is refused by the gateway with `wrong_modality` before anything is sent.
  // A single plate reading `POST /v1/chat/completions` in front of both was a
  // drawing of routing that does not exist -- and it is exactly the class of
  // wrongness that types cannot see, because every band is a band whatever it
  // answers on.
  {
    const chat = deployment('gpt-oss-120b', [id(0), id(1)])
    const tts = deployment('audio8-tts', [id(2)], 'd-tts', 'ready', 'speech')

    const textOnly = build(4, { routing, deployments: [chat] })
    ok(textOnly.entries.length === 1, 'a floor of chat deployments has one entry plate')
    ok(
      textOnly.entries[0].label === L.ENTRY_LABELS.text,
      'and it is still the chat route, unchanged',
    )
    ok(
      textOnly.exits[0].label === L.EXIT_LABELS.text,
      'and the response leaving is still the token stream',
    )

    const mixed = build(4, { routing, deployments: [chat, tts] })
    ok(mixed.entries.length === 2, 'chat beside speech is two endpoints, so two entry plates')
    ok(mixed.exits.length === 2, 'and two responses leaving, because they are not the same shape')
    const labels = mixed.entries.map((e) => e.label)
    ok(
      labels.includes('POST /v1/audio/speech'),
      'the speech plate names the route a TTS model is actually served on',
    )
    ok(
      labels[0] === L.ENTRY_LABELS.text,
      'chat first, so adding a speech deployment does not move the plate that was there',
    )
    ok(
      mixed.exits.map((e) => e.label).includes('200 audio/mpeg'),
      'and what comes back off a speech band is a file, not text/event-stream',
    )

    // Every plate has to be reachable and every band has to hang off exactly
    // one of them, or a deployment is drawn with no way in.
    const groups = L.endpointGroups(mixed.bands)
    ok(
      groups.reduce((n, [, members]) => n + members.length, 0) === mixed.bands.length,
      'every band belongs to exactly one endpoint family',
    )
    for (const band of mixed.bands) {
      ok(
        mixed.conns.some((c) => c.id === `entry-${band.id}`),
        `${band.servedName} is connected to an entry plate`,
      )
    }

    // Two plates in one rectangle read as a rendering fault, not as two
    // endpoints. Their bands interleave, so they want the same middle.
    for (const plates of [mixed.entries, mixed.exits]) {
      for (let i = 1; i < plates.length; i++) {
        ok(
          plates[i].y >= plates[i - 1].y + plates[i - 1].h,
          'stacked plates do not overlap each other',
        )
      }
    }

    // The plate grows LEFTWARD from a fixed right edge, which is what lets two
    // labels of different lengths line up where the connectors leave.
    for (const plate of mixed.entries) {
      ok(
        plate.x + plate.w === L.ENTRY_R,
        'every entry plate ends where the gutters are measured from',
      )
      ok(
        plate.w >= 2 * L.ENTRY_PAD + plate.label.length * L.ENTRY_FONT * 0.6,
        'and is wide enough for its own label',
      )
    }

    // A request block must fly out of the plate its band actually hangs off.
    const ttsBand = mixed.bands.find((b) => b.servedName === 'audio8-tts')
    const speechPlate = mixed.entries.find((e) => e.modality === 'speech')
    const flight = mixed.paths[L.localFlightKey('audio8-tts', 'd-tts')]
    ok(
      flight[0].y === speechPlate.y + speechPlate.h / 2,
      'a speech request leaves the speech plate, not the chat one',
    )
    ok(
      mixed.paths[L.outFlightKey(ttsBand.id)].slice(-1)[0].y ===
        mixed.exits.find((e) => e.modality === 'speech').y + L.EXIT_H / 2,
      'and its audio comes back to the speech exit',
    )

    // A coordinator that predates the field says nothing, and a band with no
    // modality has always been a chat band.
    ok(
      build(4, { routing, deployments: [deployment('nameless', [id(0)])] }).entries[0].label ===
        L.ENTRY_LABELS.text,
      'a band whose wire carried no modality is drawn as chat, as it always was',
    )

    // A provider's tts-1 is a remote band, and it is the same fact about it.
    const remoteTts = build(4, {
      remotes: [remote('openai', 'tts-1', { modality: 'speech' })],
      routing: [],
    })
    const remoteLabels = remoteTts.entries.map((e) => e.label)
    ok(
      remoteLabels.length === 1 && remoteLabels[0] === 'POST /v1/audio/speech',
      'a provider-served TTS model enters at the speech endpoint too',
    )
  }

  const localOnly = [{ ...routing[0], targets: [routing[0].targets[0]] }]
  const withoutRemote = build(4, { routing: localOnly, deployments: [deployment('gpt-oss-120b', [id(0), id(1)])] })
  ok(withoutRemote.provider == null, 'no remote target means NO provider bus')
  ok(withoutRemote.junctions.length === 0, 'no remote target means no junction dots')
  ok(withoutRemote.paths['gpt-oss-120b#P'] == null, 'no remote target means no provider flight path')
}

// ── What a provider serves, drawn like what we run ───────────────────────────
//
// /api/topology reports one row per model of every enabled provider. The floor
// draws the ones somebody here did something to and counts the rest, so these
// checks are mostly about what is NOT drawn: a catalogue of several hundred
// names must not become several hundred bands, and the one rule that keeps
// that true is invisible to the typechecker.
{
  const dep = deployment('gpt-oss-120b', [id(0), id(1)])
  const routing = [{
    served_name: 'gpt-oss-120b', policy: 'local_first', sticky_ttl_s: 0, flow: 'local',
    targets: [
      { target_id: 'd-gpt-oss-120b', kind: 'local', backend_url: '', weight: 0.8, outstanding: 0, healthy: true, admitting: true, strength: 1, cost_per_mtok: 0, node_ids: [id(0), id(1)] },
      { target_id: 'openrouter:gpt-oss-120b', kind: 'remote', backend_url: '', weight: 0.2, outstanding: 0, healthy: true, admitting: true, strength: 1, cost_per_mtok: 0.6 },
    ],
  }]

  // A catalogue is counted, never drawn.
  const cat = build(4, {
    deployments: [dep],
    routing,
    remotes: [remote('openrouter', 'gpt-oss-120b'), ...catalogue('openrouter', 40)],
  })
  ok(cat.bands.length === 1, 'a 40-model catalogue draws no bands of its own')
  ok(
    cat.provider.rows.length === 1 && /40 more reachable/.test(cat.provider.rows[0].text),
    'the catalogue is counted on the provider bus instead',
  )

  // A provider with its WHOLE catalogue switched on draws no band either, and
  // so has no drawn name to list. That used to reach the same clause as a
  // provider serving nothing, and the row read "nothing routed here yet · 40
  // more reachable" -- one half denying what the other counts. Every one of
  // those names is routed; none of them is drawn.
  const whole = build(4, { deployments: [dep], routing, remotes: catalogue('openrouter', 40) })
  const wholeRow = whole.provider.rows[0].text
  ok(whole.bands.length === 1, 'a wholly-catalogue provider draws no band of its own')
  ok(!/nothing routed/.test(wholeRow), 'and the bus does not claim nothing is routed: ' + wholeRow)
  ok(/40 reachable/.test(wholeRow), 'it counts what is reachable instead')

  // The clause is still there for the case it was written for: a rail that
  // exists because routing named a remote target no topology row backs.
  const none = build(4, { deployments: [dep], routing, remotes: [] })
  ok(
    /nothing routed here yet/.test(none.provider.rows[0].text),
    'a provider with no topology rows still says nothing is routed yet',
  )

  // Backing a local name puts the provider ON that name's band, not beside it.
  const backed = cat.bands[0]
  ok(backed.kind === 'local', 'a backed-up name keeps its local band')
  ok(backed.providers.join() === 'openrouter', 'the local band carries its backup provider')
  ok(/openrouter backup/.test(backed.sublabel), 'the band says which provider backs it')
  ok(
    cat.provider.rows[0].text.includes('gpt-oss-120b'),
    'the bus row names the model that provider serves',
  )

  // An alias is an operator naming a model for their clients, so it is drawn.
  const named = build(4, {
    deployments: [dep],
    routing,
    remotes: [
      remote('openrouter', 'gpt-oss-120b'),
      remote('openrouter', 'claude-ish', { upstream: 'anthropic/claude-sonnet-4.5' }),
      ...catalogue('openrouter', 40),
    ],
  })
  const aliasBand = named.bands.find((b) => b.servedName === 'claude-ish')
  ok(aliasBand != null, 'an aliased provider model gets a band')
  ok(aliasBand.kind === 'remote' && aliasBand.offCluster, 'an aliased model with no host is off cluster')
  ok(aliasBand.deploymentId === null, 'a remote band names no deployment')
  ok(
    named.bands.filter((b) => !b.offCluster).every((b) => b.y + b.h <= aliasBand.y),
    'an off-cluster band sits below every band with machines under it',
  )
  ok(aliasBand.plan === 'via openrouter', 'a remote band says who serves it where a plan would go')
  ok(
    named.paths[`claude-ish#P`] != null && named.conns.some((c) => c.id === `entry-remote:claude-ish`),
    'a remote band is entered and flown like any other',
  )

  // A short list is a list somebody chose. Every name on it is drawn.
  const short = build(4, {
    deployments: [],
    routing,
    remotes: [remote('ollama-lan', 'qwen3-8b'), remote('ollama-lan', 'llama3.2')],
  })
  ok(
    short.bands.length === 2 && short.bands.every((b) => b.kind === 'remote'),
    'a provider serving two names draws both',
  )

  // A provider that IS a machine on this roster is not off-cluster at all.
  const hosted = build(4, {
    deployments: [dep],
    routing,
    remotes: [remote('ollama-pi', 'qwen3-8b', { nodeId: id(3) })],
  })
  const hostedBand = hosted.bands.find((b) => b.servedName === 'qwen3-8b')
  ok(hostedBand.kind === 'remote' && !hostedBand.offCluster, 'a hosted provider model is not off cluster')
  ok(hostedBand.members.join() === id(3), 'a hosted model sits on the machine hosting it')
  ok(hostedBand.leads.length === 1, 'a hosted model gets a lead from its plate, like a deployment')
  ok(
    hosted.bands.filter((b) => b.offCluster).every((b) => hostedBand.y + hostedBand.h <= b.y),
    'a hosted band sits above every off-cluster band',
  )

  // A host that is not on this floor has nothing to sit on and falls back.
  const offFloor = build(2, {
    deployments: [dep],
    routing,
    remotes: [remote('ollama-pi', 'qwen3-8b', { nodeId: 'not-on-this-floor' })],
  })
  const fellBack = offFloor.bands.find((b) => b.servedName === 'qwen3-8b')
  ok(fellBack != null && fellBack.offCluster, 'a hosted model whose machine is absent falls back to off-cluster')

  // Two providers answering for one name is the merge, not two bands.
  const shared = build(4, {
    deployments: [],
    routing,
    remotes: [remote('openrouter', 'qwen3-8b'), remote('together', 'qwen3-8b')],
  })
  ok(shared.bands.length === 1, 'one served name is one band however many providers answer for it')
  ok(shared.bands[0].targetIds.length === 2, 'the band carries every target behind it')
  ok(shared.bands[0].plan === 'via 2 providers', 'and says how many answer for it')
  ok(shared.provider.rows.length === 2, 'the bus lists each provider on its own line')

  // Ids are the renderer's keys and the conn ids. Duplicates would silently
  // drop a band from the DOM and cross two flight paths -- so every kind of
  // band has to be unique against every other IN ONE LAYOUT.
  const mixed = build(4, {
    deployments: [dep, deployment('qwen3-8b', [id(2)], 'd-qwen3-8b')],
    routing,
    remotes: [
      remote('openrouter', 'gpt-oss-120b'),
      remote('openrouter', 'claude-ish', { upstream: 'anthropic/claude-sonnet-4.5' }),
      remote('ollama-pi', 'phi4', { nodeId: id(3) }),
      ...catalogue('openrouter', 40),
    ],
  })
  ok(mixed.bands.length === 4, 'local, backed-up, hosted and off-cluster bands coexist')
  ok(
    new Set(mixed.bands.map((b) => b.id)).size === mixed.bands.length,
    'every band id is unique',
  )
  ok(
    new Set(mixed.conns.map((c) => c.id)).size === mixed.conns.length,
    'every connector id is unique',
  )

  // The rule that made all of this drawable in the first place: no connector
  // may cross a plate, remote bands included.
  for (const n of [2, 4, 9]) {
    const l = build(n, {
      deployments: [dep],
      routing,
      remotes: [
        remote('openrouter', 'claude-ish', { upstream: 'anthropic/claude-sonnet-4.5' }),
        remote('ollama-pi', 'qwen3-8b', { nodeId: id(n - 1) }),
      ],
    })
    let crosses = false
    for (const c of l.conns) {
      for (const seg of segments(c.d)) {
        for (const card of l.cards) if (segmentHitsRect(seg, card)) crosses = true
      }
    }
    ok(!crosses, `n=${n} no connector crosses a plate with remote bands drawn`)
    ok(
      l.ink.y + l.ink.h >= l.provider.y + l.provider.h,
      `n=${n} the ink box contains the provider bus and its rows`,
    )
  }
}

// Selection grows the plate in place, and the rows below shift down rather
// than being overlapped.
for (const n of [6, 9, 12]) {
  const sel = build(n, { selection: { selDep: null, selNode: id(0), selLink: null } })
  const grown = sel.cards.find((c) => c.nodeId === id(0))
  const other = sel.cards.find((c) => c.nodeId !== id(0))
  ok(grown.h > other.h, `n=${n} a selected plate grows in place`)
  let collide = false
  for (let i = 0; i < sel.cards.length; i++)
    for (let j = i + 1; j < sel.cards.length; j++) if (overlaps(sel.cards[i], sel.cards[j])) collide = true
  ok(!collide, `n=${n} a grown plate does not overlap its neighbours`)
}

// Every deployment that has a placed machine gets a band, at every node count.
for (const n of [1, 2, 5, 9, 13]) {
  const deps = [deployment('a-model', [id(0)]), deployment('b-model', [id(0)])]
  const l = build(n, { deployments: deps })
  ok(l.bands.length === 2, `n=${n} every deployment gets a band`)
  ok(l.bands[0].servedName === 'a-model', `n=${n} bands are sorted by served name`)
  ok(l.bands[0].y < l.bands[1].y, `n=${n} bands stack without overlapping`)
}

// Empty cluster says so rather than drawing nothing.
ok(build(0).emptyMessage != null, 'an empty cluster carries a message')

// Narrow viewports still place everyone, and still never produce a zero-width
// card -- the bug the flow diagram had.
for (const width of [320, 480, 640, 900, 1600, 2400]) {
  const l = build(9, { width })
  ok(l.cards.length === 9 && l.cards.every((c) => c.w > 0), `width=${width} places every card at positive size`)
}

// ── Preview ──────────────────────────────────────────────────────────────────

function preview() {
  const shapes = [1, 2, 3, 4, 6, 9, 12, 16]
  const routing = [{
    served_name: 'gpt-oss-120b', policy: 'local_first', sticky_ttl_s: 0, flow: 'local',
    targets: [
      { target_id: 'd-gpt-oss-120b', kind: 'local', backend_url: '', weight: 0.8, outstanding: 0, healthy: true, admitting: true, strength: 1, cost_per_mtok: 0, node_ids: [id(0)] },
      { target_id: 'openrouter:x', kind: 'remote', backend_url: '', weight: 0.2, outstanding: 0, healthy: true, admitting: true, strength: 1, cost_per_mtok: 0.6 },
    ],
  }]
  let y = 0
  const parts = []
  let maxW = 0

  const T = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')

  // The provider furniture is in every scene, because it is the half of the
  // drawing there was previously no way to look at: a name this cluster backs
  // up, a name only a provider serves, and a catalogue that must stay counted
  // rather than drawn.
  const previewRemotes = [
    remote('openrouter', 'gpt-oss-120b'),
    remote('openrouter', 'claude-ish', { upstream: 'anthropic/claude-sonnet-4.5' }),
    ...catalogue('openrouter', 40),
  ]
  for (const n of shapes) {
    const deps = n > 1
      ? [deployment('gpt-oss-120b', [id(0), id(1)]), deployment('qwen3-8b', [id(n - 1)])]
      : [deployment('gpt-oss-120b', [id(0)])]
    const l = build(n, {
      deployments: deps,
      routing,
      width: 1200,
      remotes: [
        ...previewRemotes,
        // Hosted on the last machine on the floor: a provider that IS one of
        // these boxes, which is the case the drawing must not send off-cluster.
        ...(n > 1 ? [remote('ollama-pi', 'phi4', { nodeId: id(n - 1) })] : []),
      ],
    })
    maxW = Math.max(maxW, l.width)
    const g = []
    g.push(`<text x="4" y="10" font-size="9" fill="#5C5851">${n} machines · ${l.tier} · ${l.kind} · ${l.edges.length} links drawn, ${l.suppressedPairs} listed only</text>`)

    // One layer for every band's leads, under the whole drawing, exactly as
    // the renderer paints them: a lead crosses the bands stacked between its
    // machine and its own band, so drawing it with its band puts a hairline
    // through a neighbour's name.
    for (const b of l.bands) {
      for (const ld of b.leads) g.push(`<path d="M${ld.x} ${ld.y1} L${ld.x} ${ld.y2}" stroke="#C9C2B4"/>`)
    }
    for (const c of l.conns) {
      g.push(`<path d="${c.dRender}" fill="none" stroke="#1A1917" stroke-width="${c.weight}" opacity="${c.opacity}" stroke-linecap="square"/>`)
    }
    for (const e of l.edges) {
      if (e.bracket) {
        const b = e.bracket
        g.push(`<rect x="${b.x}" y="${b.y}" width="${b.w}" height="${b.h}" fill="#1A1917" opacity="0.16"${e.measured ? '' : ' stroke="#1A1917" stroke-width="1" stroke-dasharray="3 3"'}/>`)
        if (b.fraction > 0) g.push(`<rect x="${b.x}" y="${b.y}" width="${b.w * b.fraction}" height="${b.h}" fill="#1A1917"/>`)
      } else {
        // `dRender`, not `d`: this file is the only way to look at the floor
        // without a cluster, and a preview drawn from the proof geometry would
        // show hard corners the product does not have.
        g.push(`<path d="${e.dRender}" fill="none" stroke="#1A1917" stroke-width="${e.width}" opacity="${e.opacity}" stroke-linejoin="round"${e.dashed ? ' stroke-dasharray="4 4"' : ''}/>`)
      }
      if (e.showLabel) {
        const w = e.label.length * 5.4 + 10
        g.push(`<rect x="${e.labelAt.x - w / 2}" y="${e.labelAt.y - 9}" width="${w}" height="13" rx="2" fill="#EDE9E0"/>`)
        g.push(`<text x="${e.labelAt.x}" y="${e.labelAt.y}" font-size="9" text-anchor="middle" fill="#1A1917" font-family="monospace">${T(e.label)}</text>`)
      }
    }
    if (l.entries[0]) {
      g.push(`<rect x="${l.entries[0].x}" y="${l.entries[0].y}" width="${l.entries[0].w}" height="${l.entries[0].h}" rx="4" fill="#33302B"/>`)
      g.push(`<text x="${l.entries[0].x + L.ENTRY_PAD}" y="${l.entries[0].y + l.entries[0].h / 2 + 4}" font-size="${L.ENTRY_FONT}" fill="#EDE9E0" font-family="monospace">${T(L.ENTRY_LABEL)}</text>`)
    }
    if (l.exits[0]) {
      g.push(`<rect x="${l.exits[0].x}" y="${l.exits[0].y}" width="${l.exits[0].w}" height="${l.exits[0].h}" rx="4" fill="#33302B"/>`)
      g.push(`<text x="${l.exits[0].x + L.EXIT_PAD}" y="${l.exits[0].y + l.exits[0].h / 2 + 4}" font-size="${L.EXIT_FONT}" fill="#EDE9E0" font-family="monospace">${T(L.EXIT_LABEL)}</text>`)
    }
    for (const b of l.bands) {
      g.push(`<rect x="${b.x}" y="${b.y}" width="${b.w}" height="${b.h}" rx="3" fill="#33302B" stroke="#C9C2B4" opacity="${b.contiguous ? 1 : 0.55}"/>`)
      // Clipped exactly as the renderer clips it: a band is as wide as the
      // machines it occupies, which has nothing to do with how long its name
      // and plan are, and an unclipped preview would show a spill the app
      // does not have.
      // Scoped by scene: every scene draws the same band ids at different
      // coordinates, and a duplicate id means the FIRST clipPath in the
      // document wins for all of them -- which clips scene 2's bands against
      // scene 1's geometry and makes the preview lie about what ships.
      const cid = `bc${n}-${b.id.replace(/[^A-Za-z0-9_-]/g, '_')}`
      g.push(`<clipPath id="${cid}"><rect x="${b.x}" y="${b.y}" width="${b.w}" height="${b.h}"/></clipPath>`)
      g.push(`<g clip-path="url(#${cid})">`)
      g.push(`<text x="${b.x + 11}" y="${b.y + 15}" font-size="10" fill="#EDE9E0" font-family="monospace">${T(b.servedName)}</text>`)
      g.push(`<text x="${b.x + 11}" y="${b.y + 29}" font-size="9" fill="#A8A398" font-family="monospace">${T([b.plan, b.sublabel].filter(Boolean).join(' · '))}</text>`)
      g.push(`</g>`)
    }
    for (const c of l.cards) {
      g.push(`<rect x="${c.x}" y="${c.y}" width="${c.w}" height="${c.h}" rx="4" fill="#33302B" stroke="#C9C2B4" stroke-width="1.5"/>`)
      g.push(`<text x="${c.x + 11}" y="${c.y + 15}" font-size="9" fill="#EDE9E0" font-family="monospace">${T(c.nodeId)}</text>`)
      g.push(`<rect x="${c.x + 11}" y="${c.y + 21}" width="${c.w - 22}" height="14" rx="2" fill="#EDE9E0" opacity="0.18"/>`)
      g.push(`<rect x="${c.x + 11}" y="${c.y + 21}" width="${(c.w - 22) * 0.62}" height="14" rx="2" fill="#EDE9E0" opacity="0.85"/>`)
    }
    for (const j of l.junctions) g.push(`<circle cx="${j.x}" cy="${j.y}" r="${j.r}" fill="#1A1917" opacity="${j.opacity}"/>`)
    if (l.provider) {
      const p = l.provider
      g.push(`<rect x="${p.x + 0.5}" y="${p.y + 0.5}" width="${p.w - 1}" height="${p.h}" rx="3" fill="none" stroke="#1A1917" opacity="${p.active ? 1 : 0.55}"/>`)
      g.push(`<text x="${p.x + 11}" y="${p.y + 15}" font-size="9" fill="#1A1917" font-family="monospace">${T(p.label)}</text>`)
      g.push(`<text x="${p.x + 11}" y="${p.y + 30}" font-size="9" fill="#5C5851" font-family="monospace">${T(p.sublabel)}</text>`)
      p.rows.forEach((row, i) => {
        const ry = p.y + 43 + i * L.PROVIDER_ROW_H
        // The monogram tile IS what ships when a provider has no mark, and the
        // preview cannot fetch one, so drawing the tile keeps this file honest
        // about what the bus looks like rather than diverging from it.
        g.push(`<rect x="${p.x + L.PROVIDER_TEXT_X}" y="${ry + L.PROVIDER_LOGO_DY}" width="${L.PROVIDER_LOGO}" height="${L.PROVIDER_LOGO}" rx="2" fill="none" stroke="#1A1917"/>`)
        g.push(`<text x="${p.x + L.PROVIDER_TEXT_X + L.PROVIDER_LOGO / 2}" y="${ry}" font-size="9" text-anchor="middle" fill="#1A1917" font-family="monospace">${T((row.providerId[0] || '?').toUpperCase())}</text>`)
        g.push(`<text x="${p.x + L.PROVIDER_ROW_TEXT_X}" y="${ry}" font-size="9" fill="${row.active ? '#1A1917' : '#5C5851'}" font-family="monospace">${T(row.text)}</text>`)
      })
    }

    parts.push(`<g transform="translate(${l.offsetX} ${y})">${g.join('')}</g>`)
    y += l.ink.y + l.ink.h + 20
  }

  // Authored units, rendered at GSCALE so the preview matches what ships.
  const K = 12 / 9
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${Math.round(maxW * K)}" height="${Math.round(y * K)}" viewBox="0 0 ${maxW} ${y}"><rect width="${maxW}" height="${y}" fill="#EDE9E0"/><g font-family="sans-serif">${parts.join('')}</g></svg>`
  const path = join(uiRoot, 'layout-preview.svg')
  writeFileSync(path, svg)
  return path
}

// ── The identity line under a machine's name ────────────────────────────────
//
// A plate is captioned by node_id (or the operator's label), never by
// hostname: a worker in a --network host container reports the HOST's
// hostname, so two machines legitimately share one. When the name is hiding a
// second identity the plate reserves a line for it -- and because a row is as
// tall as its tallest plate, the whole floor pays for it or none of it does.

/** Same fixture, but every machine reports the same hostname -- the real
 *  two-containers-on-one-Spark case that started all this. */
function sharedHostname(n) {
  return nodes(n).map((node) => ({ ...node, hostname: 'spark-4d38' }))
}

function buildNodes(nodeList, opts = {}) {
  return L.layoutCluster({
    nodes: nodeList,
    links: links(nodeList.length, opts.measuredPairs ?? 1),
    deployments: [],
    routing: [],
    selection: NO_SELECTION,
    width: 1100,
    height: 640,
    order: null,
  })
}

for (const n of [2, 3, 4]) {
  const plain = buildNodes(nodes(n))
  const shared = buildNodes(sharedHostname(n))

  ok(plain.subline === 0, `n=${n} a floor where every name says it all reserves nothing`)
  ok(shared.subline === L.SUBLINE_H, `n=${n} a shared hostname reserves the identity line`)
  ok(
    shared.cards.every((c) => c.h === plain.cards[0].h + L.SUBLINE_H),
    `n=${n} EVERY plate grows, so the meters in a row still line up`,
  )

  // The line has to fit inside the plate it is drawn on: the renderer puts the
  // name at y+15, the identity at y+26 and shifts the rows below by SUBLINE_H,
  // ending at y+74+SUBLINE_H for a full-tier plate.
  ok(
    shared.cards.every((c) => 74 + L.SUBLINE_H < c.h),
    `n=${n} the shifted rows still fit inside the taller plate`,
  )

  let collide = false
  for (let i = 0; i < shared.cards.length; i++)
    for (let j = i + 1; j < shared.cards.length; j++)
      if (overlaps(shared.cards[i], shared.cards[j])) collide = true
  ok(!collide, `n=${n} taller plates still do not overlap`)
}

// Compact and chip plates have no room below the meter, so they never reserve
// it -- those tiers carry the identity in the tooltip and the node sheet.
for (const n of [6, 10]) {
  ok(
    buildNodes(sharedHostname(n)).subline === 0,
    `n=${n} tiers with no room reserve nothing`,
  )
}

// A rename is the other reason a plate's name is not its id.
{
  const renamed = nodes(3).map((node, i) =>
    i === 0 ? { ...node, label: 'Rack 2' } : node,
  )
  ok(buildNodes(renamed).subline === L.SUBLINE_H, 'a renamed machine reserves the line too')
  ok(
    buildNodes(nodes(3).map((node) => ({ ...node, label: node.node_id }))).subline === 0,
    'a label equal to the node_id is not a second identity and reserves nothing',
  )
}

// ── Every box is as wide as what it says ────────────────────────────────────
//
// Nothing on this floor is ellipsised and only the band is clipped, so a box
// too small for its own text draws that text through its neighbour: the
// machine's name ran into the model it is serving on the plate's top line,
// the served name ran under its own throughput figure on the band, and the
// bus box's rows -- which have no clip at all -- ran out of the one shape on
// the drawing that means "not yours" and across the return column.
//
// Widths are arithmetic on a 0.6em mono advance (layout `textWidth`), so this
// is checkable exactly, with no browser and no font.
{
  const W = (text, size) => text.length * size * 0.6
  const LONG = 'meta-llama/Llama-3.1-70B-Instruct'
  const cases = [
    ['one live machine', 2, [deployment('Qwen2.5-0.5B-Instruct', [id(0)])]],
    ['a long name across two', 4, [deployment(LONG, [id(0), id(1)])]],
    ['a chip floor', 12, [deployment(LONG, [id(0)])]],
    ['two deployments at once', 3, [deployment('a-model', [id(0)]), deployment(LONG, [id(2)])]],
  ]
  for (const [what, n, deps] of cases) {
    const l = build(n, { deployments: deps })

    // The plate's top line: the name from the left edge, what the machine is
    // running from the right, and air between them rather than an overlap.
    for (const c of l.cards) {
      const occupant = L.plateOccupant(
        [
          ...new Set(deps.filter((d) => d.node_ids.includes(c.nodeId)).map((d) => d.served_name)),
        ].sort(),
      )
      ok(
        11 + W(c.nodeId, 9) + 8 + W(occupant, 9) + 11 <= c.w + 0.001,
        `${what}: ${c.nodeId} says its name and its occupant without a collision`,
      )
    }
    // One width for the whole floor, or the meters in a row sit at different
    // x and the plates stop reading as one row.
    ok(
      new Set(l.cards.map((c) => c.w)).size === 1,
      `${what}: every plate on the floor has the same width`,
    )
    let collide = false
    for (let i = 0; i < l.cards.length; i++)
      for (let j = i + 1; j < l.cards.length; j++) if (overlaps(l.cards[i], l.cards[j])) collide = true
    ok(!collide, `${what}: wider plates still do not overlap`)

    // The band: name at 10px, the plan line at 9px, both clear of the readout.
    for (const b of l.bands) {
      ok(
        11 + Math.max(W(b.servedName, 10), W(L.bandSubline(b), 9)) <= L.bandLabelWidth(b.w) + 0.001,
        `${what}: ${b.servedName}'s band shows every word of both its lines`,
      )
      ok(
        b.x + b.w <= l.exits[0].x,
        `${what}: ${b.servedName}'s band stops short of the return column`,
      )
    }
  }

  // The bus box, whose rows are the longest text in the drawing and the only
  // text with nothing to cut it.
  {
    const routing = [{
      served_name: 'gpt-oss-120b', policy: 'local_first', sticky_ttl_s: 0, flow: 'local',
      targets: [
        { target_id: 'openrouter:gpt-oss-120b', kind: 'remote', backend_url: '', weight: 1, outstanding: 0, healthy: true, admitting: true, strength: 1, cost_per_mtok: 0.6 },
      ],
    }]
    const l = build(2, {
      routing,
      deployments: [deployment('gpt-oss-120b', [id(0)])],
      remotes: [
        remote('openrouter', 'gpt-oss-120b'),
        remote('openrouter', 'meta-llama/Llama-3.1-70B-Instruct', { upstream: 'meta-llama/l31-70b' }),
        remote('openrouter', 'anthracite-org/magnum-v4-72b', { upstream: 'magnum-v4' }),
        ...catalogue('openrouter', 300),
      ],
    })
    for (const row of l.provider.rows) {
      ok(
        L.PROVIDER_ROW_TEXT_X + W(row.text, 9) + 11 <= l.provider.w + 0.001,
        `the bus box holds its whole row for ${row.providerId}`,
      )
    }
    ok(
      L.PROVIDER_TEXT_X + W(l.provider.label, 9) + 11 <= l.provider.w + 0.001,
      'and its own header line',
    )
    // The floor is what grew, so everything measured from its right edge --
    // the return column, the provider drop, the trunk into the box -- moved
    // with it rather than being drawn across the box.
    ok(l.provider.x + l.provider.w < l.exits[0].x, 'the bus box stops short of the return column')
    ok(
      l.bands.every((b) => b.x + b.w <= l.provider.x + l.provider.w + 0.001),
      'and no band outgrows the floor the box spans',
    )
  }
}

const previewPath = preview()
rmSync(out, { recursive: true, force: true })

console.log(`  ${checks - failures}/${checks} checks passed`)
console.log(`  preview: ${previewPath}`)
if (failures) process.exit(1)
