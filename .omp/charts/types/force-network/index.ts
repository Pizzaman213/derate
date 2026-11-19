// Force-directed network: a d3-force simulation run synchronously for a fixed number of
// ticks from seeded random starting positions (ctx.random), so the same data and seed
// always give the same layout. It runs in an abstract unit space and is then scaled to
// fit the box, so a resize rescales the picture instead of re-simulating it.

import { forceCollide, forceLink, forceManyBody, forceSimulation, forceX, forceY, type SimulationLinkDatum, type SimulationNodeDatum } from 'd3-force'
import { defineChart, phrase, SIZES } from '../../core'
import { notes } from '../../core/helpers'
import type { Mark } from '../../core'
import { adaptGraph, netNotes, nodeColors, nodeLinkTable, normalizeNet, placeBoxes, textBox, type Box, type GNode, type Net } from '../_shared/FlowA'
import { input, options, type ForceInput, type ForceOptions } from './schema'

const net = (data: ForceInput): Net => normalizeNet(data, { undirected: true })

interface SimNode extends SimulationNodeDatum { id: string }

/** Node-ticks per layout: above this the tick count shrinks as 1/n, keeping big graphs under budget. */
const WORK = 96_000

/** Ticks actually run: `iterations`, scaled down for large graphs (never below 40). */
export const ticksFor = (n: number, iterations: number) => Math.min(iterations, Math.max(40, Math.round(WORK / Math.max(1, n))))

/** Positions in simulation units, per node id. Memoised: draw runs twice when a legend appears. */
const layouts = new WeakMap<ForceInput, Map<string, Map<string, [number, number]>>>()
function simulate(data: ForceInput, g: Net, iterations: number, seedKey: string, random: () => number): Map<string, [number, number]> {
  const key = `${seedKey}|${iterations}`
  let byData = layouts.get(data)
  const hit = byData?.get(key)
  if (hit) return hit
  const n = g.nodes.length
  const ticks = ticksFor(n, iterations)
  // a shortened run moves faster (less friction) and approximates far charge more coarsely;
  // measured on 1,200 nodes: 80 such ticks separate communities about as well as 300 default ones
  const short = ticks < iterations
  const spread = 10 * Math.sqrt(Math.max(1, n))
  const nodes: SimNode[] = g.nodes.map((d) => ({ id: d.id, x: (random() - 0.5) * 2 * spread, y: (random() - 0.5) * 2 * spread }))
  const links: Array<SimulationLinkDatum<SimNode>> = g.links.map((l) => ({ source: l.source, target: l.target }))
  const sim = forceSimulation(nodes)
    .randomSource(random)
    // cool to d3's alphaMin in exactly `ticks` steps (300 ticks = d3's default decay)
    .alphaDecay(1 - Math.pow(0.001, 1 / ticks))
    .velocityDecay(short ? 0.2 : 0.4)
    .force('link', forceLink<SimNode, SimulationLinkDatum<SimNode>>(links).id((d) => d.id).distance(30))
    .force('charge', forceManyBody<SimNode>().strength(-40).theta(short ? 1.2 : 0.9).distanceMax(short ? 200 : 400))
    .force('x', forceX<SimNode>(0).strength(0.05))
    .force('y', forceY<SimNode>(0).strength(0.05))
  // collision only matters (and is affordable) when nodes are few enough to be told apart
  if (n <= 1000) sim.force('collide', forceCollide<SimNode>(6))
  sim.stop().tick(ticks)
  const out = new Map(nodes.map((d) => [d.id, [d.x ?? 0, d.y ?? 0] as [number, number]]))
  if (!byData) layouts.set(data, (byData = new Map()))
  byData.set(key, out)
  return out
}

/** Number of connected components (isolated nodes count). */
function components(g: Net): number {
  const parent = new Map(g.nodes.map((n) => [n.id, n.id]))
  const find = (id: string): string => {
    let r = id
    while (parent.get(r) !== r) r = parent.get(r)!
    parent.set(id, r)
    return r
  }
  for (const l of g.links) parent.set(find(l.source), find(l.target))
  return new Set(g.nodes.map((n) => find(n.id))).size
}

export const forceNetwork = defineChart<ForceInput, ForceOptions>({
  id: 'force-network',
  name: 'Network graph',
  category: 'flow-hierarchy',
  intents: ['network', 'relationship'],
  summary: 'Nodes and links laid out by a force simulation: clusters, hubs and bridges show as shape.',
  input,
  options,
  shape: 'object',
  adapt: (raw, o) => adaptGraph(raw, o),
  aspect: 0.7,
  minHeight: 240,
  maxHeight: 720,
  isEmpty: (data) => net(data).nodes.length === 0,
  draw(ctx) {
    const { theme, options: o, fmt, width: W, height: H } = ctx
    const g = net(ctx.data)
    const N = g.nodes.length
    const pos = simulate(ctx.data, g, o.iterations, String(o.seed ?? 'default'), ctx.random)
    const { color, pattern, legend } = nodeColors(g, theme, 'single')
    const grouped = g.groups.length > 0
    const size = SIZES.tick

    // node radii in px: area by degree or by value
    const metric = (n: GNode) => (o.sizeBy === 'value' ? Math.max(0, n.value ?? 0) : n.degree)
    const mMax = Math.max(0, ...g.nodes.map(metric))
    const rMax = Math.max(3, Math.min(18, Math.sqrt((W * H) / Math.max(1, N)) * 0.16))
    const rMin = Math.max(1.5, Math.min(3.5, rMax * 0.3))
    const radius = (n: GNode) => (mMax > 0 ? rMin + (rMax - rMin) * Math.sqrt(metric(n) / mMax) : (rMin + rMax) / 2)

    // fit the simulation into the box, uniformly, never magnifying a tiny graph past 4x
    const pts = [...pos.values()]
    const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1])
    const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)]
    const pad = rMax + 2
    const k = Math.min(4, (W - 2 * pad) / Math.max(1e-9, x1 - x0), (H - 2 * pad) / Math.max(1e-9, y1 - y0))
    const ox = W / 2 - ((x0 + x1) / 2) * k, oy = H / 2 - ((y0 + y1) / 2) * k
    const at = new Map(g.nodes.map((n) => {
      const p = pos.get(n.id)!, r = radius(n)
      return [n.id, [Math.min(W - r - 1, Math.max(r + 1, ox + p[0] * k)), Math.min(H - r - 1, Math.max(r + 1, oy + p[1] * k))] as [number, number]]
    }))

    const marks: Mark[] = []
    const vmax = Math.max(0, ...g.links.map((l) => l.value))
    const dense = g.links.length > 1500
    for (const l of g.links) {
      const s = g.byId.get(l.source)!, t = g.byId.get(l.target)!
      const [ax, ay] = at.get(s.id)!, [bx, by] = at.get(t.id)!
      const name = `${s.label} ↔ ${t.label}`
      marks.push({
        type: 'line', role: 'link', key: `link:${l.key}`, x1: ax, y1: ay, x2: bx, y2: by,
        stroke: theme.inkSecondary, strokeOpacity: dense ? 0.18 : 0.4, strokeWidth: g.weighted && vmax > 0 ? 0.75 + 3 * Math.sqrt(l.value / vmax) : 1,
        series: grouped && s.group === t.group ? s.group : undefined,
        datum: l, label: g.weighted ? `${name}: ${fmt.value(l.value)}` : name,
        tooltip: { title: name, rows: g.weighted ? [['Weight', fmt.value(l.value)]] : [] },
      })
    }
    for (const n of g.nodes) {
      const [cx, cy] = at.get(n.id)!
      const c = color(n)
      const links = `${n.degree} link${n.degree === 1 ? '' : 's'}`
      marks.push({
        type: 'circle', role: 'node', key: `node:${n.id}`, cx, cy, r: radius(n),
        fill: c, stroke: theme.surface, strokeWidth: N > 2000 ? 0.5 : 1, pattern: pattern(n), series: grouped ? n.group : undefined,
        datum: n, label: `${n.label}: ${links}${o.sizeBy === 'value' && n.value !== null ? `, value ${fmt.value(n.value)}` : ''}`,
        tooltip: {
          title: n.group ? `${n.label} (${n.group})` : n.label,
          rows: [['Links', String(n.degree)], ...(g.weighted ? [['Total weight', fmt.value(n.inValue + n.outValue)] as [string, string]] : []), ...(n.value !== null ? [['Value', fmt.value(n.value)] as [string, string]] : [])],
          color: c,
        },
      })
    }

    // labels for the largest nodes: right, left, above or below, wherever nothing is in the way
    const ranked = [...g.nodes].sort((a, b) => metric(b) - metric(a) || a.index - b.index)
    const want = ranked.slice(0, o.labels)
    const blockers: Box[] = ranked.slice(0, Math.max(o.labels * 3, 30)).map((n) => {
      const [cx, cy] = at.get(n.id)!, r = radius(n)
      return { x: cx - r, y: cy - r, width: 2 * r, height: 2 * r }
    })
    type Opt = { box: Box; text: string; x: number; y: number; anchor: 'start' | 'middle' | 'end' }
    const minLabel = ctx.measure('Ab…', size)
    const opts: Opt[][] = want.map((n) => {
      const [cx, cy] = at.get(n.id)!, r = radius(n)
      const out: Opt[] = []
      const cands: Array<{ x: number; y: number; anchor: 'start' | 'middle' | 'end'; room: number }> = [
        { x: cx + r + 3, y: cy, anchor: 'start', room: W - (cx + r + 3) - 2 },
        { x: cx - r - 3, y: cy, anchor: 'end', room: cx - r - 3 - 2 },
        { x: cx, y: cy - r - size * 0.7, anchor: 'middle', room: 2 * Math.min(cx, W - cx) - 4 },
        { x: cx, y: cy + r + size * 0.7, anchor: 'middle', room: 2 * Math.min(cx, W - cx) - 4 },
      ]
      for (const c of cands) {
        if (c.y < size * 0.7 || c.y > H - size * 0.7) continue
        const room = Math.min(c.room, 140)
        if (room < minLabel) continue
        const text = ctx.truncate(n.label, room, size)
        out.push({ text, x: c.x, y: c.y, anchor: c.anchor, box: textBox(c.x, c.y, ctx.measure(text, size), c.anchor, size) })
      }
      return out
    })
    const pick = placeBoxes(opts.map((os) => os.map((x) => x.box)), want.map((_, i) => i), 1, blockers)
    want.forEach((n, i) => {
      const op = opts[i]![pick[i]!]
      if (!op) return
      const truncated = op.text !== n.label
      marks.push({ type: 'text', layer: 'labels', key: `label:${n.id}`, x: op.x, y: op.y, text: op.text, full: truncated ? n.label : undefined, anchor: op.anchor, baseline: 'central', fill: theme.ink, size, halo: true, inert: !truncated })
    })
    return { marks, legend, note: notes(netNotes(g.dropped)) }
  },
  describe({ data, name }) {
    const g = net(data)
    const top = g.nodes.reduce((a, b) => (b.degree > a.degree ? b : a), g.nodes[0]!)
    const parts = [`${phrase.count(g.nodes.length, 'node')} and ${phrase.count(g.links.length, 'link')}`]
    if (g.groups.length > 1) parts.push(phrase.count(g.groups.length, 'group'))
    const c = components(g)
    if (c > 1) parts.push(`${c} separate components`)
    parts.push(top && top.degree > 0 ? `most connected ${top.label} (${phrase.count(top.degree, 'link')})` : 'no links')
    return `${name}, ${parts.join(', ')}`
  },
  table: (data) => nodeLinkTable(net(data)),
})

export default forceNetwork
