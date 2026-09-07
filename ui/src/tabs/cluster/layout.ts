// Pure geometry port of mockups-next/js/cluster.js's render().
//
// No DOM, no React: this module turns cluster state into rectangles, connector
// paths and particle-flight polylines. FlowGraph.tsx is the only thing that
// ever puts any of it on screen, which is what makes this file testable with
// a plain node script (see layout.check.mjs) instead of a browser.
//
// Positions are a pure function of the data -- deployments sorted by served
// name, and a deployment's own node order left exactly as the plan produced
// it (that order is stage order for a pipeline, not a set) -- so two calls
// over the same cluster state draw identically. Panning, zooming and live
// telemetry all move independently of this, in FlowGraph and particles.ts.
//
// What did NOT come across from the mockup, and why:
//
//   - The managed-remote node tier (S.nodes[n].remote, the G2 gutter's boxes,
//     `owner`/`remotes`). TargetKind on the real wire is `local | remote`,
//     and `remote` always means a third-party provider -- there is no tier of
//     GPU nodes we manage that sit outside the measured fabric. G2 is kept as
//     a named constant so the three gutters still read "on-prem / unused /
//     provider" rather than being renumbered, but nothing is ever drawn at it.
//   - Per-node "slots" (S.nodes[n].slots[g], the memory-occupant model name
//     and per-slot percent). The wire has one memory reading per NODE
//     (NodeStateDTO.memory_used / live memory_used_pct), not one per GPU
//     slot -- there is no per-GPU-slot model on the real wire to draw.
//   - `curW()` (mockups-next/js/routing.js): a client-side recomputation of
//     routing shares per policy. Forbidden outright -- weight is a wire field
//     (RouteTarget.weight) and this module never derives one.
//   - Provider "spill"/aggregate cost. There is no provider-level aggregate
//     price or "which names spill here" list on the wire; the provider box's
//     label and spill line are built from `routing` alone (which served names
//     actually carry a `remote` target), which is also why `providers` and
//     `settings.local_only` are not inputs here: a cluster with cloud fully
//     disabled has no `remote` targets in `routing` either, so the bus
//     disappears without this module needing to know the setting directly.

import type { DeploymentDTO, RoutingConfig, TopologyEdge, TopologyNode } from '../../api/types'

/** The viewBox tracks clientWidth 1:1 in the mockup, so every font-size in
 *  the graph is its literal pixel size -- authored at 9px, under the 12px
 *  floor the rest of the panel keeps. Dividing the coordinate space by this
 *  scales every glyph, stroke and gap together without moving a single
 *  layout constant. */
export const GSCALE = 12 / 9

const NX = 152
const NW = 134
const TX = 340
const G1 = 300
/** Reserved -- see the file banner. Nothing is ever drawn here. */
export const G2 = 314
const G3 = 328
const GAP = 64
const ENTRY_W = 132
const ENTRY_H = 46

export interface Point {
  x: number
  y: number
}

export interface ClusterSelection {
  /** served_name of the selected deployment, or null. */
  selDep: string | null
  /** node_id of the selected node, or null. */
  selNode: string | null
  /** `"a~b"`, sorted -- state/selection's `linkKey` format. */
  selLink: string | null
}

export interface ClusterLayoutInput {
  deployments: DeploymentDTO[]
  nodes: TopologyNode[]
  links: TopologyEdge[]
  routing: RoutingConfig[]
  selection: ClusterSelection
  /** The graph element's clientWidth in CSS pixels (0/undefined before the
   *  first layout pass; treated the same as the mockup's `g.clientWidth||700`). */
  width: number
}

export type ClusterBox =
  | { kind: 'entry'; x: number; y: number; w: number; h: number }
  | {
      kind: 'name'
      x: number
      y: number
      w: number
      h: number
      servedName: string
      selected: boolean
    }
  | {
      kind: 'node'
      x: number
      y: number
      w: number
      h: number
      nodeId: string
      servedName: string
      selected: boolean
      /** Drawn taller, with the live telemetry overlay, because this row's
       *  selected node is one of its endpoints. */
      expanded: boolean
    }
  | {
      kind: 'bracket'
      x: number
      y: number
      w: number
      h: number
      /** Always a valid `state/selection` link key for this exact pair,
       *  whether or not a topology edge record exists for it yet. */
      linkKey: string
      measured: boolean
      /** 0..1 against the 40 GB/s tensor-parallel threshold. Zero for an
       *  unmeasured pair -- that is a blank bar, never a zero-value fill. */
      fraction: number
      /** "{ar} of 40 GB/s" or "never measured" -- never a number the pair
       *  was not actually measured at. */
      caption: string
      note: string
    }
  | {
      kind: 'provider'
      x: number
      y: number
      w: number
      h: number
      active: boolean
      label: string
      sublabel: string
    }
  | { kind: 'junction'; x: number; y: number; r: number; opacity: number }

export interface ClusterConn {
  id: string
  d: string
  weight: number
  dashed: boolean
  opacity: number
}

export interface ClusterLabel {
  x: number
  y: number
  text: string
  size?: number
  /** A knockout rect drawn behind this label only -- for the one label that
   *  sits directly on top of the routing-boundary rule (mockups-next/js/
   *  cluster.js draws `rect{x:TX-4,y:bd-9,...,fill:'var(--panel)'}` before
   *  this text for exactly that reason). Column-header labels at y=9 sit
   *  over blank space and never carry one. */
  plate?: { x: number; y: number; w: number; h: number }
}

export interface ClusterLayout {
  width: number
  height: number
  boxes: ClusterBox[]
  conns: ClusterConn[]
  /** Keyed exactly as particles.ts looks them up: `${servedName}#L${i}` for
   *  the i'th local hop in plan order, `${servedName}#P` for the provider
   *  tap. Only present when the corresponding box/tap was actually drawn. */
  paths: Record<string, Point[]>
  labels: ClusterLabel[]
  /** y of the "routing boundary" rule, exposed for anything that wants to
   *  draw relative to it without re-deriving the row layout. */
  boundaryY: number
}

function edgeKey(a: string, b: string): string {
  return [a, b].sort().join('~')
}

/** The one definition of "measured" a bracket/chip/tally is allowed to use:
 *  the wire's own `measured` flag AND an actual figure to show for it. An
 *  edge that claims `measured: true` but carries no `all_reduce_gbps` has
 *  nothing to draw and must read exactly like an edge that was never probed
 *  at all -- SelectionRail's default-rail tally uses this too, so the count
 *  next to the chips can never disagree with what the chips themselves say. */
export function edgeMeasured(edge: Pick<TopologyEdge, 'measured' | 'all_reduce_gbps'> | undefined): boolean {
  return edge?.measured === true && edge.all_reduce_gbps != null
}

function findEdge(links: TopologyEdge[], a: string, b: string): TopologyEdge | undefined {
  return links.find((l) => (l.src === a && l.dst === b) || (l.src === b && l.dst === a))
}

/** `fitGraph`'s width floor, folded into the pure function. 640 is a
 *  *rendered* minimum, so it is divided by GSCALE too -- left undivided it
 *  out-clamps the scale below ~900px and the type falls back under 12px. */
function graphWidth(containerWidth: number): number {
  return Math.max(Math.round(640 / GSCALE), Math.round((containerWidth || 700) / GSCALE))
}

interface Row {
  dep: DeploymentDTO
  local: { x: number; y: number; w: number; h: number; nodeId: string }[]
  ny: number | null
}

export function layoutCluster(input: ClusterLayoutInput): ClusterLayout {
  const GW = graphWidth(input.width)
  const TW = Math.max(240, GW - TX)
  const knownNodes = new Set(input.nodes.map((n) => n.node_id))

  const boxes: ClusterBox[] = []
  const conns: ClusterConn[] = []
  const paths: Record<string, Point[]> = {}
  const labels: ClusterLabel[] = []

  const deployments = [...input.deployments].sort((a, b) =>
    a.served_name.localeCompare(b.served_name),
  )

  const rows: Row[] = []
  let y = 36

  for (const dep of deployments) {
    // A deployment mid-placement (no node yet) has nowhere honest to be
    // drawn -- there is no managed-remote tier left to fall back to.
    const ids = dep.node_ids.filter((id) => knownNodes.has(id))
    if (ids.length === 0) continue

    const k = ids.length
    const anySelected = input.selection.selNode != null && ids.includes(input.selection.selNode)
    const h = k >= 2 ? (anySelected ? 100 : 70) : anySelected ? 84 : 54
    const bw = (TW - (k - 1) * GAP) / k
    const local = ids.map((nodeId, i) => ({ x: TX + i * (bw + GAP), y, w: bw, h, nodeId }))
    y += h + (k >= 2 ? 28 : 10)
    rows.push({ dep, local, ny: local[0]!.y + local[0]!.h / 2 })
  }

  const placed = rows.filter((r) => r.ny != null)
  const boundaryY = y + 14
  let ry = boundaryY + 20
  const provY = ry

  const hasProviderBus = input.routing.some((cfg) => cfg.targets.some((t) => t.kind === 'remote'))
  // The mockup reserves a flat 46px for the provider row regardless of the
  // 38px box drawn inside it (js/cluster.js: `ry+=46`) -- an 8px clearance
  // below the box, not 0. ENTRY_H happens to equal that same 46, purely
  // because both this row and the entry box share one height constant.
  if (hasProviderBus) ry += ENTRY_H

  const height = ry + 14

  labels.push({ x: 0, y: 9, text: 'endpoint' })
  labels.push({ x: NX, y: 9, text: 'served names' })
  labels.push({ x: TX, y: 9, text: 'targets · measured, plans possible' })

  const ey = placed.length ? placed[Math.floor(placed.length / 2)]!.ny! : 120
  boxes.push({ kind: 'entry', x: 0, y: ey - 23, w: ENTRY_W, h: ENTRY_H })

  for (const row of rows) {
    if (row.ny == null) continue
    const ny = row.ny
    const servedName = row.dep.served_name
    const selectedDep = input.selection.selDep === servedName
    const k = row.local.length
    const anySelected = k >= 2 ? row.local[0]!.h === 100 : row.local[0]!.h === 84

    conns.push({
      id: `${servedName}-entry`,
      d: `M${ENTRY_W} ${ey} H${NX - 6} V${ny} H${NX}`,
      weight: 5,
      dashed: false,
      opacity: 1,
    })
    boxes.push({ kind: 'name', x: NX, y: ny - 18, w: NW, h: 36, servedName, selected: selectedDep })

    row.local.forEach((b, i) => {
      const my = b.y + b.h / 2
      conns.push({
        id: `${servedName}-local-${i}`,
        d: `M${NX + NW} ${ny} H${G1} V${my} H${b.x}`,
        weight: 5,
        dashed: false,
        opacity: 1,
      })
      paths[`${servedName}#L${i}`] = [
        { x: ENTRY_W, y: ey },
        { x: NX - 6, y: ey },
        { x: NX - 6, y: ny },
        { x: NX + NW, y: ny },
        { x: G1, y: ny },
        { x: G1, y: my },
        { x: b.x + b.w / 2, y: my },
      ]
      boxes.push({
        kind: 'node',
        x: b.x,
        y: b.y,
        w: b.w,
        h: b.h,
        nodeId: b.nodeId,
        servedName,
        selected: b.nodeId === input.selection.selNode,
        expanded: anySelected,
      })
    })

    for (let i = 0; i + 1 < row.local.length; i++) {
      const a = row.local[i]!
      const b = row.local[i + 1]!
      const mid = a.x + a.w
      const gap = b.x - mid
      const edge = findEdge(input.links, a.nodeId, b.nodeId)
      const measured = edgeMeasured(edge)
      const fraction = measured ? Math.min(1, edge!.all_reduce_gbps! / 40) : 0
      const caption = measured ? `${edge!.all_reduce_gbps!.toFixed(1)} of 40 GB/s` : 'never measured'
      boxes.push({
        kind: 'bracket',
        x: mid,
        y: a.y + 14,
        w: gap,
        h: 18,
        linkKey: edgeKey(a.nodeId, b.nodeId),
        measured,
        fraction,
        caption,
        note: `${a.nodeId} ↔ ${b.nodeId}`,
      })
    }

    if (hasProviderBus) {
      const cfg = input.routing.find((c) => c.served_name === servedName)
      const remoteIdx = cfg?.targets.findIndex((t) => t.kind === 'remote') ?? -1
      const hasRemote = remoteIdx >= 0
      const active = selectedDep && hasRemote && (cfg!.targets[remoteIdx]!.weight ?? 0) > 0
      conns.push({
        id: `${servedName}-provider`,
        d: `M${NX + NW} ${ny} H${G3}`,
        weight: hasRemote ? 2.5 : 1,
        dashed: true,
        opacity: active ? 1 : hasRemote ? 0.5 : 0.22,
      })
      if (hasRemote) {
        boxes.push({ kind: 'junction', x: G3, y: ny, r: 3, opacity: active ? 1 : 0.5 })
        paths[`${servedName}#P`] = [
          { x: ENTRY_W, y: ey },
          { x: NX - 6, y: ey },
          { x: NX - 6, y: ny },
          { x: NX + NW, y: ny },
          { x: G3, y: ny },
          { x: G3, y: provY + 19 },
          { x: TX + TW / 2, y: provY + 19 },
        ]
      }
    }
  }

  if (hasProviderBus) {
    const top = placed.length ? Math.min(...placed.map((r) => r.ny!)) : provY
    conns.push({
      id: 'provider-rail',
      d: `M${G3} ${top} V${provY + 19} H${TX}`,
      weight: 1,
      dashed: true,
      opacity: 0.5,
    })

    const remoteTargets = input.routing.flatMap((cfg) =>
      cfg.targets.filter((t) => t.kind === 'remote').map((t) => ({ cfg, t })),
    )
    const providerIds = new Set(remoteTargets.map(({ t }) => t.target_id.split(':')[0] ?? t.target_id))
    const routedNames = [
      ...new Set(
        input.routing
          .filter((cfg) => cfg.targets.some((t) => t.kind === 'remote'))
          .map((cfg) => cfg.served_name),
      ),
    ]
    const label =
      providerIds.size === 1 ? [...providerIds][0]! : `${providerIds.size} providers`
    const anyActive = rows.some((r) => {
      if (r.ny == null || input.selection.selDep !== r.dep.served_name) return false
      const cfg = input.routing.find((c) => c.served_name === r.dep.served_name)
      const idx = cfg?.targets.findIndex((t) => t.kind === 'remote') ?? -1
      return idx >= 0 && (cfg!.targets[idx]!.weight ?? 0) > 0
    })

    boxes.push({
      kind: 'provider',
      x: TX,
      y: provY,
      w: TW,
      h: 38,
      active: anyActive,
      // Mockup line 1 is `${id} · third party · $${cost}/Mtok · no telemetry`
      // -- there is no provider-level aggregate price on the wire to fill
      // the middle clause with, but "no telemetry" is not a cost figure and
      // stays on this line rather than migrating to the second.
      label: `${label} · third party · no telemetry`,
      sublabel: `proxy for any served name · ${
        routedNames.length ? `routed for ${routedNames.join(', ')}` : 'none routed here yet'
      }`,
    })
  }

  const boundaryLabel = 'routing boundary · managed, never sharded'
  labels.push({
    x: TX,
    y: boundaryY + 4,
    text: boundaryLabel,
    size: 11,
    plate: { x: TX - 4, y: boundaryY - 9, w: boundaryLabel.length * 5.3 + 8, h: 18 },
  })

  return { width: GW, height, boxes, conns, paths, labels, boundaryY }
}
