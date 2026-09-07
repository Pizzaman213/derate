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

import { execFileSync } from 'node:child_process'
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const uiRoot = join(here, '..', '..', '..')
const out = mkdtempSync(join(tmpdir(), 'layout-check-'))
const bundle = join(out, 'layout.mjs')

execFileSync(
  join(uiRoot, 'node_modules', '.bin', 'esbuild'),
  [join(here, 'layout.ts'), '--bundle', '--format=esm', `--outfile=${bundle}`, '--log-level=warning'],
  { stdio: 'inherit' },
)

const L = await import(bundle)

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

function deployment(servedName, nodeIds, depId = `d-${servedName}`) {
  return {
    deployment_id: depId,
    served_name: servedName,
    model_id: `org/${servedName}`,
    runtime: 'vllm',
    state: 'ready',
    context_length: 8192,
    max_concurrent_seqs: 8,
    started_at: 0,
    last_error: null,
    node_ids: nodeIds,
    plan: plan(1, nodeIds.length),
    fit: null,
  }
}

const NO_SELECTION = { selDep: null, selNode: null, selLink: null }

function build(n, opts = {}) {
  return L.layoutCluster({
    nodes: nodes(n),
    links: links(n, opts.measuredPairs ?? 1),
    deployments: opts.deployments ?? [],
    routing: opts.routing ?? [],
    selection: opts.selection ?? NO_SELECTION,
    width: opts.width ?? 1100,
    height: opts.height ?? 640,
    order: opts.order ?? null,
  })
}

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

// A drag rewrites the arrangement without losing or duplicating anyone.
{
  const before = build(5).arrangement
  const after = L.moveToSlot(before, before[4], 0)
  ok(after[0] === before[4], 'moveToSlot puts the node where it was dropped')
  ok(after.length === before.length && new Set(after).size === after.length, 'moveToSlot loses nobody')
  ok(L.moveToSlot(before, 'nope', 0) === before, 'moveToSlot ignores an unknown id')
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
      ...(l.entry ? [[l.entry.x, l.entry.x + l.entry.w]] : []),
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
    ok(l.offsetX >= 0, `${n} machines: centring never pushes the drawing off the left edge`)
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
    const boxes = [...l.cards, ...l.bands, ...(l.entry ? [l.entry] : []), ...(l.provider ? [l.provider] : [])]
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

  const solo = build(4, { deployments: [deployment('gpt-oss-120b', [id(0)])] })
  ok(solo.bands.length === 1, 'a single-node deployment gets a band too')
  ok(solo.bands[0].w === solo.card.w, 'a solo band spans exactly its one machine')
  ok(solo.entry != null, 'a solo deployment still gets an entry box')
  ok(solo.conns.some((c) => c.id.startsWith('entry-')), 'the entry box connects to the solo band')

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
  ok(l.paths['gpt-oss-120b#L0'] != null && l.paths['gpt-oss-120b#L1'] != null, 'a path per routing target')
  // cfg.targets order is d-b then d-a, so #L0 must be the SOLO one.
  // cfg.targets order is d-b then d-a, so #L0 must be the SOLO one -- shorter,
  // because it has no inter-machine hop, but it still starts at the endpoint.
  ok(l.paths['gpt-oss-120b#L0'].length === 6, '#L0 follows cfg.targets order, not deployment order')
  ok(l.paths['gpt-oss-120b#L1'].length > 6, '#L1 walks the whole pipeline')
  ok(l.paths['gpt-oss-120b#L0'][0].x === 132, 'a flight starts at the entry box')
  // The flight has to end on the last stage, not wherever the geometry happened
  // to point.
  const pipeline = l.paths['gpt-oss-120b#L1']
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
  ok(withRemote.boundaryY != null, 'the provider bus brings the routing boundary with it')
  ok(withRemote.junctions.length === 1, 'a configured remote target gets a junction dot')
  ok(withRemote.paths['gpt-oss-120b#P'] != null, 'a remote target gets a provider flight path')

  const localOnly = [{ ...routing[0], targets: [routing[0].targets[0]] }]
  const withoutRemote = build(4, { routing: localOnly, deployments: [deployment('gpt-oss-120b', [id(0), id(1)])] })
  ok(withoutRemote.provider == null, 'no remote target means NO provider bus')
  ok(withoutRemote.boundaryY == null, 'no provider bus means no routing boundary either')
  ok(withoutRemote.junctions.length === 0, 'no remote target means no junction dots')
  ok(withoutRemote.paths['gpt-oss-120b#P'] == null, 'no remote target means no provider flight path')
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

  for (const n of shapes) {
    const deps = n > 1
      ? [deployment('gpt-oss-120b', [id(0), id(1)]), deployment('qwen3-8b', [id(n - 1)])]
      : [deployment('gpt-oss-120b', [id(0)])]
    const l = build(n, { deployments: deps, routing, width: 1200 })
    maxW = Math.max(maxW, l.width)
    const g = []
    g.push(`<text x="4" y="10" font-size="9" fill="#5C5851">${n} machines · ${l.tier} · ${l.kind} · ${l.edges.length} links drawn, ${l.suppressedPairs} listed only</text>`)

    for (const c of l.conns) {
      g.push(`<path d="${c.d}" fill="none" stroke="#1A1917" stroke-width="${c.weight}" opacity="${c.opacity}"${c.dashed ? ' stroke-dasharray="5 4"' : ' stroke-linecap="square"'}/>`)
    }
    for (const e of l.edges) {
      if (e.bracket) {
        const b = e.bracket
        g.push(`<rect x="${b.x}" y="${b.y}" width="${b.w}" height="${b.h}" fill="#1A1917" opacity="0.16"${e.measured ? '' : ' stroke="#1A1917" stroke-width="1" stroke-dasharray="3 3"'}/>`)
        if (b.fraction > 0) g.push(`<rect x="${b.x}" y="${b.y}" width="${b.w * b.fraction}" height="${b.h}" fill="#1A1917"/>`)
      } else {
        g.push(`<path d="${e.d}" fill="none" stroke="#1A1917" stroke-width="${e.width}" opacity="${e.opacity}"${e.dashed ? ' stroke-dasharray="4 4"' : ''}/>`)
      }
      if (e.showLabel) {
        const w = e.label.length * 5.4 + 10
        g.push(`<rect x="${e.labelAt.x - w / 2}" y="${e.labelAt.y - 9}" width="${w}" height="13" rx="2" fill="#EDE9E0"/>`)
        g.push(`<text x="${e.labelAt.x}" y="${e.labelAt.y}" font-size="9" text-anchor="middle" fill="#1A1917" font-family="monospace">${T(e.label)}</text>`)
      }
    }
    if (l.entry) {
      g.push(`<rect x="${l.entry.x}" y="${l.entry.y}" width="${l.entry.w}" height="${l.entry.h}" rx="4" fill="#33302B"/>`)
      g.push(`<text x="${l.entry.x + 12}" y="${l.entry.y + l.entry.h / 2 + 4}" font-size="10" fill="#EDE9E0" font-family="monospace">POST /v1/chat/</text>`)
    }
    for (const b of l.bands) {
      for (const ld of b.leads) g.push(`<path d="M${ld.x} ${ld.y1} L${ld.x} ${ld.y2}" stroke="#C9C2B4"/>`)
      g.push(`<rect x="${b.x}" y="${b.y}" width="${b.w}" height="${b.h}" rx="3" fill="#33302B" stroke="#C9C2B4" opacity="${b.contiguous ? 1 : 0.55}"/>`)
      g.push(`<text x="${b.x + 11}" y="${b.y + 15}" font-size="10" fill="#EDE9E0" font-family="monospace">${T(b.servedName)}</text>`)
      g.push(`<text x="${b.x + 11}" y="${b.y + 29}" font-size="9" fill="#A8A398" font-family="monospace">${T([b.plan, b.sublabel].filter(Boolean).join(' · '))}</text>`)
    }
    for (const c of l.cards) {
      g.push(`<rect x="${c.x}" y="${c.y}" width="${c.w}" height="${c.h}" rx="4" fill="#33302B" stroke="#C9C2B4" stroke-width="1.5"/>`)
      g.push(`<text x="${c.x + 11}" y="${c.y + 15}" font-size="9" fill="#EDE9E0" font-family="monospace">${T(c.nodeId)}</text>`)
      g.push(`<rect x="${c.x + 11}" y="${c.y + 21}" width="${c.w - 22}" height="14" rx="2" fill="#EDE9E0" opacity="0.18"/>`)
      g.push(`<rect x="${c.x + 11}" y="${c.y + 21}" width="${(c.w - 22) * 0.62}" height="14" rx="2" fill="#EDE9E0" opacity="0.85"/>`)
    }
    for (const j of l.junctions) g.push(`<circle cx="${j.x}" cy="${j.y}" r="${j.r}" fill="#1A1917" opacity="${j.opacity}"/>`)
    if (l.boundaryY != null) {
      g.push(`<line x1="0" y1="${l.boundaryY}" x2="${l.width}" y2="${l.boundaryY}" stroke="#C9C2B4"/>`)
    }
    if (l.provider) {
      const p = l.provider
      g.push(`<rect x="${p.x + 0.5}" y="${p.y + 0.5}" width="${p.w - 1}" height="${p.h}" rx="3" fill="none" stroke="#1A1917" opacity="${p.active ? 1 : 0.55}"/>`)
      g.push(`<text x="${p.x + 11}" y="${p.y + 15}" font-size="9" fill="#1A1917" font-family="monospace">${T(p.label)}</text>`)
      g.push(`<text x="${p.x + 11}" y="${p.y + 30}" font-size="9" fill="#5C5851" font-family="monospace">${T(p.sublabel)}</text>`)
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

const previewPath = preview()
rmSync(out, { recursive: true, force: true })

console.log(`  ${checks - failures}/${checks} checks passed`)
console.log(`  preview: ${previewPath}`)
if (failures) process.exit(1)
