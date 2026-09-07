// Pure geometry for the machine floor.
//
// No DOM, no React: this module turns cluster state into machine plates, link
// geometry, deployment bands, the request-flow furniture and particle-flight
// polylines. ClusterGraph.tsx is the only thing that ever puts any of it on
// screen, which is what makes this file testable with a plain node script
// (layout.check.mjs) instead of a browser.
//
// Two lineages meet here, deliberately.
//
// The STRUCTURE is a machine floor: one plate per node in /api/topology,
// running or not. It replaced a diagram that iterated DEPLOYMENTS, so a
// machine hosting nothing was never drawn, a machine serving two deployments
// was drawn twice, and the only links on the canvas were those between
// adjacent nodes inside one deployment row. On a live two-node cluster with
// one unmeasured link that rendered one card and zero links.
//
// The LOOK and the FLOW FURNITURE are mockups-next/js/cluster.js: the entry
// box, the served-name plate (which is this file's band), the bandwidth
// bracket, the routing boundary and the provider bus, all authored in that
// file's units. Everything below is in AUTHORED units and the viewBox divides
// by GSCALE, so 9 authored px renders at the 12px floor the rest of the panel
// keeps and every glyph, stroke, gap and radius scales together.
//
// Positions are a pure function of the node set plus `order` (the persisted
// drag arrangement, passed in rather than read from localStorage so this file
// stays pure). The same cluster at the same viewport draws identically every
// time -- H-ui.md:154 bans a force simulation, and a machine you cannot learn
// the position of is worse than one that is merely plain.
//
// Channel grammar, one meaning each (AUDIT-2026-09-06.md:145):
//
//   width   measured all-reduce bandwidth, and nothing else
//   dash    never measured, or crosses the routing boundary
//   opacity deprioritized
//   colour  something is WRONG -- healthy is a --rule hairline
//
// so an unmeasured link is a fixed hairline carrying no figure, never a thin
// line implying a small number we do not have.

import type { DeploymentDTO, RoutingConfig, TopologyEdge, TopologyNode } from '../../api/types'
import { planShortFromDegrees } from '../../format'

export interface Point {
  x: number
  y: number
}

export type Tier = 'full' | 'compact' | 'chip'
export type FloorKind = 'grid' | 'ring'

/** The viewBox is deliberately smaller than the element, so everything renders
 *  1.333x larger than authored. The graph is authored on a 9px type scale and
 *  the panel's floor is 12px; dividing the coordinate space scales every
 *  glyph, stroke, gap and radius together and not one constant has to move. */
export const GSCALE = 12 / 9

/** Plate size per tier, in authored units. These ARE the mockup's three box
 *  heights: 84 is its selected solo node, 54 its compact solo node, 38 its
 *  remote row. Constant per tier and NEVER divided by the node count -- the
 *  old flow diagram sized boxes as `(TW - (k-1)*GAP)/k`, which goes negative
 *  at five nodes in a row and silently erases them. */
export const CARD: Record<Tier, { w: number; h: number }> = {
  full: { w: 138, h: 84 },
  compact: { w: 102, h: 54 },
  chip: { w: 78, h: 38 },
}

/** 64 at full tier is the mockup's hard span gap: it is the bandwidth
 *  bracket's channel, and every bracket constant is measured from it. */
const GAP_X: Record<Tier, number> = { full: 64, compact: 48, chip: 36 }
const GAP_Y: Record<Tier, number> = { full: 40, compact: 36, chip: 26 }
const MAX_COLS: Record<Tier, number> = { full: 4, compact: 4, chip: 6 }

export const MARGIN = 16
/** Space above the first row for links that arc between non-adjacent plates.
 *  Only reserved when one actually will. */
export const ARC_HEADROOM = 54

// ── The three columns, left to right ─────────────────────────────────────────
/** Entry plate: `POST /v1/chat/completions`. */
const ENTRY_W = 132
const ENTRY_H = 46
/** Elbow gutter: one entry fans out to N bands through here. */
const GUTTER_ENTRY = 140
/** Provider rail: one vertical run tapping every band, then one trunk into the
 *  bus. N edges converging on a point would state the same relation N times. */
const GUTTER_RAIL = 156
/** Left edge of the machine floor and of everything that spans it. */
const FLOOR_X = 172

const BAND_H = 36
const BAND_GAP = 8
const BAND_LEAD_SPACE = 20
const PROVIDER_H = 38

/** Above this a grid of plates stops being legible and the floor becomes a
 *  ring. The brief contemplates two to four machines. */
export const RING_ABOVE = 12

/** Bandwidth at which tensor parallel becomes viable. A link drawn at full
 *  weight is a link where TP is on the table. */
export const TP_THRESHOLD = 40

/** How far outside its box the renderer draws a plate's hover and selection
 *  ring. The ink box carries it so a fitted drawing cannot clip its own rings. */
const RING_PAD = 3

export interface ClusterSelection {
  selDep: string | null
  selNode: string | null
  selLink: string | null
}

export interface ClusterLayoutInput {
  nodes: TopologyNode[]
  links: TopologyEdge[]
  deployments: DeploymentDTO[]
  routing: RoutingConfig[]
  selection: ClusterSelection
  /** The graph element's clientWidth in CSS pixels (0 before the first layout
   *  pass, treated as 700). Divided by GSCALE to reach authored units. */
  width: number
  /** The graph element's clientHeight in CSS pixels (0 before the first layout
   *  pass, treated as 420). Only its ratio to `width` is used: the viewBox is
   *  the element's own box, so the drawing can be fitted into all of it. */
  height: number
  /** Persisted drag arrangement: node ids in slot order. null = default.
   *  Passed in, never read from storage here, so this stays a pure function. */
  order: string[] | null
}

export interface PlacedCard {
  nodeId: string
  x: number
  y: number
  w: number
  h: number
  /** Index into `slots`. What a drag rearranges. */
  slot: number
  row: number
  col: number
  selected: boolean
  /** Selection grows a plate in place, as the mockup does, rather than opening
   *  anything over the drawing. A grown plate shows the thermal and hardware
   *  lines; `tier` is the floor's tier, this is what the plate can actually
   *  hold. */
  bodyTier: Tier
}

export interface ClusterBracket {
  x: number
  y: number
  w: number
  h: number
  /** 0..1 against TP_THRESHOLD. Zero for an unmeasured pair -- a blank track,
   *  never a zero-value fill. */
  fraction: number
  /** The invisible taller rect that makes a 7-unit bar clickable. */
  hitY: number
  hitH: number
}

export interface ClusterEdge {
  linkKey: string
  src: string
  dst: string
  /** `bracket` between facing plates -- the mockup's signature bandwidth
   *  element, drawn in the channel between them. `path` for anything that has
   *  to arc, because a bracket cannot span a curve. */
  kind: 'bracket' | 'path'
  /** Empty for a bracket. */
  d: string
  bracket: ClusterBracket | null
  /** Measured: scaled against TP_THRESHOLD. Unmeasured: a fixed hairline,
   *  because width means bandwidth and we do not have one. */
  width: number
  dashed: boolean
  opacity: number
  measured: boolean
  /** "10.2 of 40 GB/s" or "never measured". Never a number that was not
   *  measured, and the threshold is named rather than left implicit. */
  label: string
  labelAt: Point
  showLabel: boolean
  selected: boolean
  aria: string
}

export interface ClusterBand {
  deploymentId: string
  servedName: string
  x: number
  y: number
  w: number
  h: number
  /** Every occupied plate sits on one row, in consecutive columns. When false
   *  the bar is drawn open, with a tick per occupied plate, so a plate inside
   *  the span that is not a member never reads as one. */
  contiguous: boolean
  ticks: number[]
  members: string[]
  /** Vertical leads from an occupied plate down to the band. Empty when the
   *  members are not all on one row -- a lead from the first row would cross
   *  the second. */
  leads: { x: number; y1: number; y2: number }[]
  plan: string
  sublabel: string
  degraded: boolean
  selected: boolean
  /** Centre y. The entry connector and the provider tap both meet the band
   *  here. */
  ny: number
}

/** Entry box, provider rail and taps. Flat 5-unit square-cap runs for the
 *  measured on-premises path; 2.5 butt-cap dashes for anything crossing the
 *  routing boundary, where there is no measured fabric to draw a figure from. */
export interface ClusterConn {
  id: string
  d: string
  weight: number
  dashed: boolean
  opacity: number
}

export interface ClusterProvider {
  x: number
  y: number
  w: number
  h: number
  active: boolean
  label: string
  sublabel: string
}

export interface ClusterLayout {
  tier: Tier
  kind: FloorKind
  /** Authored units. The renderer's viewBox, which is the graph element's own
   *  box rather than the drawing's extent: the element fills the floor, and
   *  the renderer's fit transform scales the ink up into all of it. Both are
   *  derived from the same measured ratio, so the viewBox aspect always
   *  equals the element's and preserveAspectRatio never letterboxes. */
  width: number
  height: number
  /** Bounding box of the drawn boxes, in layout coordinates -- i.e. BEFORE the
   *  renderer's translate(offsetX 0). The fit transform frames this. */
  ink: { x: number; y: number; w: number; h: number }
  /** Authored units to slide the whole drawing right so its ink is centred in
   *  the viewBox. The floor centres the machines inside the space left of the
   *  entry box and the bands, which is not the same thing: at two machines on
   *  a wide panel that leaves a third of the panel empty on the right and the
   *  drawing hugging the left edge. Everything is laid out from x = 0 as
   *  before -- this is the last step, applied by the renderer as one translate
   *  so no coordinate in here (paths, slots, drop targeting) has to know about
   *  it. */
  offsetX: number
  card: { w: number; h: number }
  cards: PlacedCard[]
  edges: ClusterEdge[]
  bands: ClusterBand[]
  conns: ClusterConn[]
  /** null when there is no deployment to enter. */
  entry: { x: number; y: number; w: number; h: number } | null
  /** y of the routing-boundary rule, or null when nothing crosses it. */
  boundaryY: number | null
  provider: ClusterProvider | null
  junctions: { x: number; y: number; r: number; opacity: number }[]
  /** Top-left of every slot, in slot order. Drop targeting reads this. */
  slots: Point[]
  arrangement: string[]
  /** Keyed exactly as particles.ts looks them up: `${servedName}#L${i}` for
   *  the i'th ROUTING TARGET of that served name, in `cfg.targets` order. */
  paths: Record<string, Point[]>
  emptyMessage: string | null
  suppressedPairs: number
}

export function edgeKey(a: string, b: string): string {
  return [a, b].sort().join('~')
}

/** The one definition of "measured" a link, chip or tally is allowed to use:
 *  the wire's own `measured` flag AND an actual figure to show for it. An edge
 *  that claims `measured: true` but carries no `all_reduce_gbps` has nothing
 *  to draw and must read exactly like one that was never probed -- the rail's
 *  tally uses this too, so the count next to the chips can never disagree with
 *  what the chips themselves say. */
export function edgeMeasured(edge: Pick<TopologyEdge, 'measured' | 'all_reduce_gbps'> | undefined): boolean {
  return edge?.measured === true && edge.all_reduce_gbps != null
}

/** Thickness carries the measurement, scaled against the tensor-parallel
 *  threshold. Authored units, so 4.5 renders at 6. */
export function edgeWidth(gbps: number): number {
  return 1.2 + 4.5 * Math.min(1, gbps / TP_THRESHOLD)
}

export function tierFor(n: number): Tier {
  if (n <= 4) return 'full'
  if (n <= 8) return 'compact'
  return 'chip'
}

/** Plate width for a caption, from the real font metric. IBM Plex Mono's
 *  advance is 0.6em, so 0.6 x 9 = 5.4 exactly; 5.3 approximates 11px Plex
 *  Sans. Both plus 5 units of padding each side. */
export function plateWidth(text: string, mono = true): number {
  return text.length * (mono ? 5.4 : 5.3) + 10
}

const centerOf = (c: PlacedCard): Point => ({ x: c.x + c.w / 2, y: c.y + c.h / 2 })

function sampleQuadratic(p0: Point, c: Point, p1: Point, steps = 12): Point[] {
  const pts: Point[] = []
  for (let i = 0; i <= steps; i++) {
    const t = i / steps
    const u = 1 - t
    pts.push({
      x: u * u * p0.x + 2 * u * t * c.x + t * t * p1.x,
      y: u * u * p0.y + 2 * u * t * c.y + t * t * p1.y,
    })
  }
  return pts
}

interface Geometry {
  facing: boolean
  d: string
  mid: Point
  pts: Point[]
}

/** Facing plates get a straight segment between their near edges -- that
 *  channel is where the bracket goes. Anything else arcs, so a link never
 *  runs underneath a machine it does not touch. */
function edgeGeometry(a: PlacedCard, b: PlacedCard, kind: FloorKind): Geometry {
  if (kind === 'ring') {
    const c1 = centerOf(a)
    const c2 = centerOf(b)
    return {
      facing: false,
      d: `M${c1.x} ${c1.y} L${c2.x} ${c2.y}`,
      mid: { x: (c1.x + c2.x) / 2, y: (c1.y + c2.y) / 2 },
      pts: [c1, c2],
    }
  }

  if (a.row === b.row) {
    const [l, r] = a.x <= b.x ? [a, b] : [b, a]
    if (Math.abs(a.col - b.col) === 1) {
      const y = l.y + l.h / 2
      const p0 = { x: l.x + l.w, y }
      const p1 = { x: r.x, y }
      return { facing: true, d: `M${p0.x} ${p0.y} L${p1.x} ${p1.y}`, mid: { x: (p0.x + p1.x) / 2, y }, pts: [p0, p1] }
    }
    const p0 = { x: l.x + l.w / 2, y: l.y }
    const p1 = { x: r.x + r.w / 2, y: r.y }
    const span = Math.abs(p1.x - p0.x)
    const lift = Math.min(ARC_HEADROOM * 1.25, 26 + span * 0.075)
    const c = { x: (p0.x + p1.x) / 2, y: Math.min(p0.y, p1.y) - lift }
    return {
      facing: false,
      d: `M${p0.x} ${p0.y} Q${c.x} ${c.y} ${p1.x} ${p1.y}`,
      mid: { x: c.x, y: (p0.y + 2 * c.y + p1.y) / 4 },
      pts: sampleQuadratic(p0, c, p1),
    }
  }

  const [t, bm] = a.y <= b.y ? [a, b] : [b, a]
  const p0 = { x: t.x + t.w / 2, y: t.y + t.h }
  const p1 = { x: bm.x + bm.w / 2, y: bm.y }
  if (t.col === bm.col && Math.abs(t.row - bm.row) === 1) {
    return { facing: true, d: `M${p0.x} ${p0.y} L${p1.x} ${p1.y}`, mid: { x: p0.x, y: (p0.y + p1.y) / 2 }, pts: [p0, p1] }
  }
  const bulge = p0.x === p1.x ? t.w * 0.6 : (p1.x - p0.x) * 0.15
  const c = { x: (p0.x + p1.x) / 2 + bulge, y: (p0.y + p1.y) / 2 }
  return {
    facing: false,
    d: `M${p0.x} ${p0.y} Q${c.x} ${c.y} ${p1.x} ${p1.y}`,
    mid: { x: (p0.x + 2 * c.x + p1.x) / 4, y: (p0.y + 2 * c.y + p1.y) / 4 },
    pts: sampleQuadratic(p0, c, p1),
  }
}

/** Coordinator first, then lexicographic. Still a pure function of the node
 *  set, and the machine everything else hangs off lands in the first slot. */
function defaultArrangement(nodes: TopologyNode[]): string[] {
  return [...nodes]
    .sort(
      (a, b) =>
        (a.role === 'coordinator' ? 0 : 1) - (b.role === 'coordinator' ? 0 : 1) ||
        a.node_id.localeCompare(b.node_id),
    )
    .map((n) => n.node_id)
}

/** Reconcile the persisted drag order against who is actually here. Ids of
 *  departed nodes are dropped from the RESULT but kept in storage, so a node
 *  that leaves and rejoins returns to its slot; nodes that joined since the
 *  drag are appended in default order rather than jumping to the front. */
export function reconcileOrder(nodes: TopologyNode[], order: string[] | null): string[] {
  const fallback = defaultArrangement(nodes)
  if (!order || order.length === 0) return fallback
  const present = new Set(fallback)
  const known = new Set(order)
  return [...order.filter((id) => present.has(id)), ...fallback.filter((id) => !known.has(id))]
}

export function layoutCluster(input: ClusterLayoutInput): ClusterLayout {
  // Authored units throughout. 480 is the rendered 640 floor, divided.
  const GW = Math.max(Math.round(640 / GSCALE), Math.round((input.width || 700) / GSCALE))
  // The viewBox must match the element's aspect exactly or preserveAspectRatio
  // letterboxes it and the dead space this whole arrangement exists to remove
  // comes straight back. Deriving GH from GW and the measured ratio keeps the
  // two equal by construction, even where GW is pinned at its 480 floor on a
  // narrow panel and no longer tracks the measured width.
  const GH = Math.max(1, Math.round((GW * (input.height || 420)) / (input.width || 700)))
  const arrangement = reconcileOrder(input.nodes, input.order)
  const n = arrangement.length
  const tier = tierFor(n)
  const card = CARD[tier]
  const kind: FloorKind = n > RING_ABOVE ? 'ring' : 'grid'

  const cards: PlacedCard[] = []
  const slots: Point[] = []
  const edges: ClusterEdge[] = []
  const bands: ClusterBand[] = []
  const conns: ClusterConn[] = []
  const junctions: { x: number; y: number; r: number; opacity: number }[] = []
  const paths: Record<string, Point[]> = {}

  if (n === 0) {
    return {
      // No ink to frame: this path renders the message as a <p>, not the SVG.
      tier, kind, width: GW, height: GH, ink: { x: 0, y: 0, w: GW, h: GH }, offsetX: 0, card,
      cards, edges, bands, conns, junctions, slots, paths,
      entry: null, boundaryY: null, provider: null,
      arrangement,
      emptyMessage: 'No machines yet. A Spark on this network appears here on its own.',
      suppressedPairs: 0,
    }
  }

  // Pairs a deployment is actually relying on. Needed before placement, since
  // whether any link arcs decides how much headroom the floor reserves.
  const present = new Set(arrangement)
  const spannedPairs = new Set<string>()
  for (const dep of input.deployments) {
    const ids = dep.node_ids.filter((idv) => present.has(idv))
    for (let i = 0; i + 1 < ids.length; i++) spannedPairs.add(edgeKey(ids[i]!, ids[i + 1]!))
  }
  const showsPair = (l: TopologyEdge) =>
    edgeMeasured(l) ||
    spannedPairs.has(edgeKey(l.src, l.dst)) ||
    n <= 4 ||
    input.selection.selNode === l.src ||
    input.selection.selNode === l.dst

  const floorAvail = Math.max(card.w, GW - FLOOR_X - MARGIN)
  let floorRight = FLOOR_X + floorAvail
  let machineBottom: number

  if (kind === 'grid') {
    const gx = GAP_X[tier]
    const gy = GAP_Y[tier]
    const fits = Math.floor((floorAvail + gx) / (card.w + gx))
    const cols = Math.max(1, Math.min(n, MAX_COLS[tier], Number.isFinite(fits) ? fits : 1))
    const rows = Math.ceil(n / cols)
    const gridW = cols * card.w + (cols - 1) * gx
    const originX = FLOOR_X + Math.max(0, (floorAvail - gridW) / 2)

    const slotOf = new Map(arrangement.map((nodeId, i) => [nodeId, i]))
    const willArc = input.links.some((l) => {
      const i = slotOf.get(l.src)
      const j = slotOf.get(l.dst)
      if (i == null || j == null) return false
      if (Math.floor(i / cols) !== Math.floor(j / cols)) return false
      if (Math.abs((i % cols) - (j % cols)) === 1) return false
      return showsPair(l)
    })
    const topPad = willArc ? ARC_HEADROOM : MARGIN

    // Selection GROWS the plate in place rather than opening anything over the
    // drawing, so a row is only as tall as its tallest plate and the rows
    // below shift down. That is the mockup's own rule ("height is data-driven
    // so the selected node grows in place") and it is why row tops accumulate
    // instead of being row * pitch.
    const bodyTierOf = (nodeId: string): Tier =>
      nodeId === input.selection.selNode && tier !== 'full'
        ? tier === 'chip'
          ? 'compact'
          : 'full'
        : tier
    const rowHeights = Array.from({ length: rows }, (_, r) =>
      Math.max(
        ...arrangement
          .slice(r * cols, r * cols + cols)
          .map((nodeId) => CARD[bodyTierOf(nodeId)].h),
      ),
    )
    const rowTop = (r: number) =>
      topPad + rowHeights.slice(0, r).reduce((a, h) => a + h + gy, 0)

    arrangement.forEach((nodeId, i) => {
      const row = Math.floor(i / cols)
      const col = i % cols
      const bodyTier = bodyTierOf(nodeId)
      const x = originX + col * (card.w + gx)
      const y = rowTop(row)
      slots.push({ x, y })
      cards.push({
        nodeId, x, y,
        w: card.w,
        h: CARD[bodyTier].h,
        slot: i, row, col,
        selected: nodeId === input.selection.selNode,
        bodyTier,
      })
    })

    floorRight = Math.max(originX + gridW, FLOOR_X + card.w)
    machineBottom = rowTop(rows - 1) + rowHeights[rows - 1]!
  } else {
    const radius = Math.max(120, (n * (card.w + 30)) / (2 * Math.PI))
    const w = Math.ceil(2 * (radius + card.w / 2))
    const h = Math.ceil(2 * (radius + card.h / 2))
    const cx = FLOOR_X + Math.max(w, floorAvail) / 2
    const cy = MARGIN + h / 2
    arrangement.forEach((nodeId, i) => {
      const angle = -Math.PI / 2 + (i / n) * 2 * Math.PI
      const x = cx + radius * Math.cos(angle) - card.w / 2
      const y = cy + radius * Math.sin(angle) - card.h / 2
      slots.push({ x, y })
      cards.push({
        nodeId, x, y, w: card.w, h: card.h, slot: i, row: 0, col: i,
        selected: nodeId === input.selection.selNode,
        bodyTier: tier,
      })
    })
    floorRight = Math.max(FLOOR_X + w, FLOOR_X + card.w)
    machineBottom = MARGIN + h
  }

  const placed = new Map(cards.map((c) => [c.nodeId, c]))

  // ── Links ────────────────────────────────────────────────────────────────
  //
  // /api/topology returns a COMPLETE graph -- every pair, measured or not
  // (control_plane/gateway/internal_api.py:79-99). That is 6 edges at four
  // machines but 66 at twelve, of which realistically one or two carry a
  // figure. Drawing all 66 is a hairball that hides the one that matters.
  let suppressedPairs = 0

  for (const link of input.links) {
    const a = placed.get(link.src)
    const b = placed.get(link.dst)
    if (!a || !b || a.nodeId === b.nodeId) continue
    if (!showsPair(link)) {
      suppressedPairs++
      continue
    }

    const key = edgeKey(link.src, link.dst)
    const measured = edgeMeasured(link)
    const geo = edgeGeometry(a, b, kind)
    const selected = input.selection.selLink === key
    const relied = spannedPairs.has(key)
    const incident = input.selection.selNode === link.src || input.selection.selNode === link.dst
    const fraction = measured ? Math.min(1, link.all_reduce_gbps! / TP_THRESHOLD) : 0
    const label = measured ? `${link.all_reduce_gbps!.toFixed(1)} of ${TP_THRESHOLD} GB/s` : 'never measured'

    // The mockup's bracket: a 7-tall bar filling the channel between two
    // facing plates, top edge at a.y+19 so it lines up with the meters inside
    // them. Only where there IS a channel -- a bracket cannot span a curve.
    const horizontal = a.row === b.row && kind === 'grid'
    const bracket: ClusterBracket | null =
      geo.facing && horizontal
        ? {
            x: Math.min(a.x + a.w, b.x + b.w),
            y: Math.min(a.y, b.y) + 19,
            w: Math.abs(a.x <= b.x ? b.x - (a.x + a.w) : a.x - (b.x + b.w)),
            h: 7,
            fraction,
            hitY: Math.min(a.y, b.y) + 14,
            hitH: 18,
          }
        : null

    edges.push({
      linkKey: key,
      src: link.src,
      dst: link.dst,
      kind: bracket ? 'bracket' : 'path',
      d: bracket ? '' : geo.d,
      bracket,
      width: measured ? edgeWidth(link.all_reduce_gbps!) : 1,
      dashed: !measured,
      // Opacity is the deprioritized channel. An unmeasured pair nothing runs
      // over is background; one a deployment depends on is not.
      opacity: measured ? 1 : relied || selected || incident ? 0.7 : 0.35,
      measured,
      label,
      labelAt: bracket
        ? { x: bracket.x + bracket.w / 2, y: Math.min(a.y, b.y) - 8 }
        : geo.mid,
      showLabel: measured || relied || selected || incident,
      selected,
      // `medium` is declared required in types.ts but the gateway omits it for
      // a pair it has never probed, so it is only spoken when it is there.
      aria: measured
        ? `Link ${link.src} to ${link.dst}, ${link.all_reduce_gbps!.toFixed(1)} gigabytes per second all-reduce${link.medium ? ` over ${link.medium}` : ''}`
        : `Link ${link.src} to ${link.dst}${link.medium ? ` over ${link.medium}` : ''}, never measured`,
    })
  }

  // Two captions landing on top of each other is worse than one of them
  // moving. Nudge in draw order; the first one placed keeps its spot.
  const taken: Point[] = []
  for (const e of edges) {
    if (!e.showLabel) continue
    let guard = 0
    while (taken.some((p) => Math.abs(p.x - e.labelAt.x) < 44 && Math.abs(p.y - e.labelAt.y) < 13) && guard < 6) {
      e.labelAt = { x: e.labelAt.x, y: e.labelAt.y - 14 }
      guard++
    }
    taken.push(e.labelAt)
  }

  // ── Bands: one per deployment, including single-node ones ────────────────
  //
  // The band IS the mockup's served-name plate. A solo deployment gets one
  // spanning its single machine, because that is what gives it somewhere to
  // sit in the request flow -- the entry box connects to bands, never to
  // machines.
  const bandTop = machineBottom + BAND_LEAD_SPACE
  const drawable = input.deployments
    .filter((d) => d.node_ids.some((id) => placed.has(id)))
    .sort((a, b) => a.served_name.localeCompare(b.served_name) || a.deployment_id.localeCompare(b.deployment_id))

  drawable.forEach((dep, i) => {
    const occupied = dep.node_ids
      .map((id) => placed.get(id))
      .filter((c): c is PlacedCard => c != null)
      .sort((p, q) => p.row - q.row || p.col - q.col)
    const y = bandTop + i * (BAND_H + BAND_GAP)
    const sameRow = kind === 'grid' && occupied.every((c) => c.row === occupied[0]!.row)
    const cols = occupied.map((c) => c.col)
    const consecutive = cols.every((c, j) => j === 0 || c === cols[j - 1]! + 1)
    const contiguous = sameRow && consecutive

    const left = sameRow ? Math.min(...occupied.map((c) => c.x)) : FLOOR_X
    const right = sameRow ? Math.max(...occupied.map((c) => c.x + c.w)) : floorRight

    bands.push({
      deploymentId: dep.deployment_id,
      servedName: dep.served_name,
      x: left,
      y,
      w: right - left,
      h: BAND_H,
      ny: y + BAND_H / 2,
      contiguous,
      ticks: occupied.map((c) => c.x + c.w / 2),
      members: occupied.map((c) => c.nodeId),
      leads: sameRow ? occupied.map((c) => ({ x: c.x + c.w / 2, y1: c.y + c.h, y2: y })) : [],
      plan: dep.plan ? planShortFromDegrees(dep.plan) : '',
      sublabel: contiguous ? '' : occupied.map((c) => c.nodeId).join(', '),
      degraded: dep.state === 'degraded',
      selected: input.selection.selDep === dep.served_name,
    })
  })

  const bandBottom = bands.length ? bandTop + bands.length * (BAND_H + BAND_GAP) - BAND_GAP : machineBottom

  // ── Entry box, and the fan-out to the bands ──────────────────────────────
  //
  // The entry connects to BANDS, not to machines: that is the mockup's own
  // architecture (endpoint -> served name -> targets), and it is the only
  // routing that cannot cross a plate, since a connector aimed at column two
  // would have to pass through column one.
  let entry: { x: number; y: number; w: number; h: number } | null = null
  let ey = 0
  if (bands.length > 0) {
    ey = bands[Math.floor(bands.length / 2)]!.ny
    entry = { x: 0, y: ey - ENTRY_H / 2, w: ENTRY_W, h: ENTRY_H }
    for (const band of bands) {
      conns.push({
        id: `entry-${band.deploymentId}`,
        d: `M${ENTRY_W} ${ey} H${GUTTER_ENTRY} V${band.ny} H${band.x}`,
        weight: 5,
        dashed: false,
        opacity: 1,
      })
    }
  }

  // ── Routing boundary and the provider bus ────────────────────────────────
  //
  // A provider is ONE endpoint that can serve anything, so it is drawn as a
  // bus: a rail tapping every running model and a single trunk into the box.
  // It is outlined, never filled -- it is not a machine you own, and H-ui.md
  // is emphatic that no provider is ever drawn as one.
  const remoteOf = (servedName: string) => {
    const cfg = input.routing.find((c) => c.served_name === servedName)
    const t = cfg?.targets.find((x) => x.kind === 'remote')
    return t ?? null
  }
  const hasProviderBus = input.routing.some((cfg) => cfg.targets.some((t) => t.kind === 'remote'))

  let boundaryY: number | null = null
  let provider: ClusterProvider | null = null

  if (hasProviderBus) {
    boundaryY = bandBottom + 14
    const provY = boundaryY + 20
    let anyActive = false

    for (const band of bands) {
      const t = remoteOf(band.servedName)
      const active = t != null && (t.weight ?? 0) > 0 && band.selected
      if (active) anyActive = true
      conns.push({
        id: `tap-${band.deploymentId}`,
        d: `M${band.x} ${band.ny} H${GUTTER_RAIL}`,
        // Tap weight is the honest part: 2.5 means the provider is a
        // configured target for that name; a 1-unit hairline means merely
        // reachable through the proxy.
        weight: t ? 2.5 : 1,
        dashed: true,
        opacity: active ? 1 : t ? 0.5 : 0.22,
      })
      if (t) {
        junctions.push({ x: GUTTER_RAIL, y: band.ny, r: 3, opacity: active ? 1 : 0.5 })
        paths[`${band.servedName}#P`] = [
          { x: ENTRY_W, y: ey },
          { x: GUTTER_ENTRY, y: ey },
          { x: GUTTER_ENTRY, y: band.ny },
          { x: GUTTER_RAIL, y: band.ny },
          { x: GUTTER_RAIL, y: provY + 19 },
          { x: FLOOR_X + (floorRight - FLOOR_X) / 2, y: provY + 19 },
        ]
      }
    }

    const top = bands.length ? Math.min(...bands.map((b) => b.ny)) : provY
    conns.push({
      id: 'provider-rail',
      d: `M${GUTTER_RAIL} ${top} V${provY + 19} H${FLOOR_X}`,
      weight: 1,
      dashed: true,
      opacity: 0.5,
    })

    const remoteTargets = input.routing.flatMap((cfg) => cfg.targets.filter((t) => t.kind === 'remote'))
    const providerIds = new Set(remoteTargets.map((t) => t.target_id.split(':')[0] ?? t.target_id))
    const routedNames = [
      ...new Set(
        input.routing.filter((cfg) => cfg.targets.some((t) => t.kind === 'remote')).map((cfg) => cfg.served_name),
      ),
    ]
    provider = {
      x: FLOOR_X,
      y: provY,
      w: floorRight - FLOOR_X,
      h: PROVIDER_H,
      active: anyActive,
      // The mockup's line 1 carries an aggregate price. There is no
      // provider-level price on the wire to fill that clause with, but
      // "no telemetry" is not a cost figure and stays on this line.
      label: `${providerIds.size === 1 ? [...providerIds][0]! : `${providerIds.size} providers`} · third party · no telemetry`,
      sublabel: `proxy for any served name · ${
        routedNames.length ? `routed for ${routedNames.join(', ')}` : 'none routed here yet'
      }`,
    }
  }

  // ── Particle flight paths ────────────────────────────────────────────────
  //
  // One path per ROUTING TARGET of a served name, in cfg.targets order, so
  // particles.ts's pickPath can line its wire weights up with them one for one
  // and never has to guess. A flight enters at the endpoint, reaches the
  // served name, then walks the deployment's pipeline in node_ids order --
  // which is stage order, not a set -- so a token visibly crosses the link the
  // plan turned on.
  const bandOf = new Map(bands.map((b) => [b.deploymentId, b]))
  const byName = new Map<string, DeploymentDTO[]>()
  for (const dep of drawable) {
    const list = byName.get(dep.served_name) ?? []
    list.push(dep)
    byName.set(dep.served_name, list)
  }

  for (const [servedName, deps] of byName) {
    const cfg = input.routing.find((c) => c.served_name === servedName)
    const targetOrder = cfg ? cfg.targets.filter((t) => t.kind === 'local').map((t) => t.target_id) : []
    const ordered = [...deps].sort((a, b) => {
      const ia = targetOrder.indexOf(a.deployment_id)
      const ib = targetOrder.indexOf(b.deployment_id)
      if (ia !== ib) return (ia < 0 ? Number.MAX_SAFE_INTEGER : ia) - (ib < 0 ? Number.MAX_SAFE_INTEGER : ib)
      return a.deployment_id.localeCompare(b.deployment_id)
    })

    ordered.forEach((dep, i) => {
      const band = bandOf.get(dep.deployment_id)
      const hops = dep.node_ids.map((id) => placed.get(id)).filter((c): c is PlacedCard => c != null)
      if (!band || hops.length === 0) return

      const first = hops[0]!
      const leadX = first.x + first.w / 2
      const pts: Point[] = [
        { x: ENTRY_W, y: ey },
        { x: GUTTER_ENTRY, y: ey },
        { x: GUTTER_ENTRY, y: band.ny },
        { x: band.x, y: band.ny },
        { x: leadX, y: band.ny },
        centerOf(first),
      ]
      for (let h = 0; h + 1 < hops.length; h++) {
        const seg = edgeGeometry(hops[h]!, hops[h + 1]!, kind).pts
        const next = centerOf(hops[h + 1]!)
        const last = seg[seg.length - 1]!
        const forward =
          Math.hypot(last.x - next.x, last.y - next.y) <= Math.hypot(seg[0]!.x - next.x, seg[0]!.y - next.y)
        pts.push(...(forward ? seg : [...seg].reverse()), next)
      }
      paths[`${servedName}#L${i}`] = pts
    })
  }

  // Centre the ink. The extremes are the boxes, not the connectors: a
  // connector only ever runs between two of them, and the provider rail's own
  // trunk sits inside the provider box's span.
  const boxes: { x: number; y: number; w: number; h: number }[] = [...cards, ...bands]
  if (entry) boxes.push(entry)
  if (provider) boxes.push(provider)
  const inkL = Math.min(...boxes.map((b) => b.x))
  const inkR = Math.max(...boxes.map((b) => b.x + b.w))
  // Same reasoning down the other axis, which the fit transform needs. Padded
  // by the hover/selection ring the renderer draws 3 units outside every box,
  // so a fitted drawing does not clip its own rings against the viewBox edge.
  const inkT = Math.min(...boxes.map((b) => b.y)) - RING_PAD
  const inkB = Math.max(...boxes.map((b) => b.y + b.h)) + RING_PAD
  const ink = { x: inkL - RING_PAD, y: inkT, w: inkR - inkL + 2 * RING_PAD, h: inkB - inkT }
  // Never negative: a drawing wider than the viewBox stays pinned left, where
  // panning can still reach the rest of it.
  const offsetX = Math.max(0, Math.round(GW / 2 - (inkL + inkR) / 2))

  return {
    tier, kind, width: GW, height: GH, ink, offsetX, card,
    cards, edges, bands, conns, junctions, slots, paths,
    entry, boundaryY, provider,
    arrangement,
    emptyMessage: null,
    suppressedPairs,
  }
}

/** Move `nodeId` into `slot`, shifting everything between. What a drag and the
 *  keyboard reorder both write. */
export function moveToSlot(arrangement: string[], nodeId: string, slot: number): string[] {
  const from = arrangement.indexOf(nodeId)
  if (from < 0) return arrangement
  const to = Math.max(0, Math.min(arrangement.length - 1, slot))
  if (from === to) return arrangement
  const next = [...arrangement]
  next.splice(from, 1)
  next.splice(to, 0, nodeId)
  return next
}

/** The slot whose centre is nearest a point, for drop targeting. */
export function nearestSlot(slots: Point[], card: { w: number; h: number }, at: Point): number {
  let best = -1
  let bestD = Infinity
  slots.forEach((s, i) => {
    const d = Math.hypot(s.x + card.w / 2 - at.x, s.y + card.h / 2 - at.y)
    if (d < bestD) {
      bestD = d
      best = i
    }
  })
  return best
}
