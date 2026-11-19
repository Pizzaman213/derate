// Sankey diagram: nodes as columns of bars, links as bands whose width is the flow.
// Layout is d3-sankey; labels sit beside their node, truncated to the gap between
// columns and dropped (tooltip still has them) where they would collide.

import { sankey as d3sankey, sankeyCenter, sankeyJustify, sankeyLeft, sankeyLinkHorizontal, sankeyRight, type SankeyLink, type SankeyNode } from 'd3-sankey'
import { defineChart, phrase, SIZES } from '../../core'
import { notes } from '../../core/helpers'
import type { Mark } from '../../core'
import { adaptGraph, arrow, linkTable, netNotes, nodeColors, normalizeNet, placeBoxes, textBox, type Box, type GLink, type GNode, type Net } from '../_shared/FlowA'
import { input, options, type SankeyInput, type SankeyOptions } from './schema'

interface SN { id: string; node: GNode }
interface SL { link: GLink }
type LaidNode = SankeyNode<SN, SL>
type LaidLink = SankeyLink<SN, SL>

const ALIGN = { justify: sankeyJustify, left: sankeyLeft, right: sankeyRight, center: sankeyCenter } as const

const net = (data: SankeyInput): Net => normalizeNet(data)

export const sankey = defineChart<SankeyInput, SankeyOptions>({
  id: 'sankey',
  name: 'Sankey diagram',
  category: 'flow-hierarchy',
  intents: ['flow'],
  summary: 'Flows between stages as bands whose width is the quantity moved.',
  input,
  options,
  shape: 'object',
  adapt: (raw, o) => adaptGraph(raw, o),
  aspect: 0.6,
  minHeight: 220,
  isEmpty(data) {
    const g = net(data)
    if (g.links.length) return false
    if (!data.links.length) return true
    return g.dropped.zeros === data.links.length ? 'All flows are zero' : 'No positive flows to draw'
  },
  draw(ctx) {
    const { theme, options: o, fmt, width: W, height: H } = ctx
    const g = net(ctx.data)
    const used = g.nodes.filter((n) => n.inValue + n.outValue > 0)
    const isolated = g.nodes.length - used.length
    const nodeWidth = W < 500 ? 8 : 12
    const run = (pad: number, iterations: number) =>
      d3sankey<SN, SL>()
        .nodeId((d) => d.id)
        .nodeAlign(ALIGN[o.nodeAlign])
        .nodeWidth(nodeWidth)
        .nodePadding(pad)
        .iterations(iterations)
        .extent([[1, 4], [W - 1, H - 4]])({
          nodes: used.map((n) => ({ id: n.id, node: n })),
          links: g.links.map((l) => ({ source: l.source, target: l.target, value: l.value, link: l })),
        })
    // padding from the busiest column: generous when there is room, never collapsing the bars
    const probe = run(0, 0)
    const perColumn = new Map<number, number>()
    for (const n of probe.nodes) perColumn.set(n.x0 ?? 0, (perColumn.get(n.x0 ?? 0) ?? 0) + 1)
    const maxCol = Math.max(1, ...perColumn.values())
    const pad = maxCol > 1 ? Math.max(1, Math.min(14, ((H - 8) * 0.35) / (maxCol - 1))) : 14
    const laid = run(pad, 6)
    const nodes = laid.nodes as LaidNode[]
    const links = laid.links as LaidLink[]

    const { color, pattern, legend } = nodeColors(g, theme)
    const grouped = g.groups.length > 0
    const marks: Mark[] = []
    const nodeOf = (n: LaidNode | string | number): LaidNode => n as LaidNode
    for (const l of links) {
      const s = nodeOf(l.source), t = nodeOf(l.target)
      const w = Math.max(1, l.width ?? 1)
      const x0 = s.x1 ?? 0, x1 = t.x0 ?? 0, y0 = l.y0 ?? 0, y1 = l.y1 ?? 0
      const d = sankeyLinkHorizontal()(l)
      if (!d) continue
      const share = s.node.outValue > 0 ? l.value / s.node.outValue : 0
      const name = arrow(s.node.label, t.node.label)
      marks.push({
        type: 'path', role: 'link', key: `link:${l.link.key}`, d, fill: 'none', stroke: color(s.node), strokeWidth: w, strokeOpacity: 0.42,
        cx: (x0 + x1) / 2, cy: (y0 + y1) / 2, box: { x: x0, y: Math.min(y0, y1) - w / 2, width: Math.max(1, x1 - x0), height: Math.abs(y1 - y0) + w },
        series: grouped ? s.node.group : undefined,
        datum: l.link, label: `${name}: ${fmt.value(l.value)}`,
        tooltip: { title: name, rows: [['Flow', fmt.value(l.value)], [`Share of ${s.node.label}`, fmt.pct(share)]], color: color(s.node) },
      })
    }
    for (const n of nodes) {
      const x = n.x0 ?? 0, y = n.y0 ?? 0, h = Math.max(1, (n.y1 ?? 0) - y), v = n.value ?? 0
      const c = color(n.node)
      marks.push({
        type: 'rect', role: 'node', key: `node:${n.id}`, x, y, width: Math.max(1, (n.x1 ?? 0) - x), height: h, rx: 1,
        fill: c, pattern: pattern(n.node), series: grouped ? n.node.group : undefined,
        datum: n.node, label: `${n.node.label}: ${fmt.value(v)}`,
        tooltip: { title: n.node.group ? `${n.node.label} (${n.node.group})` : n.node.label, rows: [['Total', fmt.value(v)], ['In', fmt.value(n.node.inValue)], ['Out', fmt.value(n.node.outValue)]], color: c },
      })
    }

    // labels: beside the node, inside the gap to the neighbouring column
    const size = SIZES.tick
    const columns = [...new Set(nodes.map((n) => n.x0 ?? 0))].sort((a, b) => a - b)
    const gapRight = (n: LaidNode) => {
      const next = columns.find((c) => c > (n.x0 ?? 0) + 0.5)
      return (next ?? W) - (n.x1 ?? 0) - (next === undefined ? 4 : 10)
    }
    const gapLeft = (n: LaidNode) => {
      const prev = [...columns].reverse().find((c) => c < (n.x0 ?? 0) - 0.5)
      return (n.x0 ?? 0) - (prev === undefined ? 4 : prev + nodeWidth + 10)
    }
    const minLabel = ctx.measure('Ab…', size)
    type Opt = { box: Box; text: string; x: number; y: number; anchor: 'start' | 'end' }
    const opts: Opt[][] = nodes.map((n) => {
      const cy = Math.min(H - size * 0.7, Math.max(size * 0.7, ((n.y0 ?? 0) + (n.y1 ?? 0)) / 2))
      const value = o.showValues ? ` ${fmt.value(n.value ?? 0)}` : ''
      const valueW = value ? ctx.measure(value, size) : 0
      const right = (n.x0 ?? 0) + nodeWidth / 2 < W / 2
      const sides: Array<{ anchor: 'start' | 'end'; avail: number }> = []
      const r = { anchor: 'start' as const, avail: gapRight(n) }, l = { anchor: 'end' as const, avail: gapLeft(n) }
      sides.push(...(right ? [r, l] : [l, r]))
      const out: Opt[] = []
      for (const sd of sides)
        for (const frac of [1, 0.5]) {
          const avail = sd.avail * frac - valueW
          if (avail < minLabel) continue
          const text = ctx.truncate(n.node.label, avail, size) + value
          const x = sd.anchor === 'start' ? (n.x1 ?? 0) + 6 : (n.x0 ?? 0) - 6
          out.push({ text, x, y: cy, anchor: sd.anchor, box: textBox(x, cy, ctx.measure(text, size), sd.anchor, size) })
        }
      return out
    })
    const order = nodes.map((_, i) => i).sort((a, b) => (nodes[b]!.value ?? 0) - (nodes[a]!.value ?? 0))
    const blockers = nodes.map((n) => ({ x: n.x0 ?? 0, y: n.y0 ?? 0, width: (n.x1 ?? 0) - (n.x0 ?? 0), height: (n.y1 ?? 0) - (n.y0 ?? 0) }))
    const pick = placeBoxes(opts.map((os) => os.map((x) => x.box)), order, 1, blockers)
    nodes.forEach((n, i) => {
      const k = pick[i]!
      if (k < 0) return
      const op = opts[i]![k]!
      const truncated = !op.text.startsWith(n.node.label)
      marks.push({
        type: 'text', layer: 'labels', key: `label:${n.id}`, x: op.x, y: op.y, text: op.text, full: truncated ? n.node.label : undefined,
        anchor: op.anchor, baseline: 'central', fill: theme.ink, size, halo: true, inert: !truncated,
      })
    })
    return {
      marks,
      legend,
      note: notes(netNotes(g.dropped, [[isolated, `unconnected node${isolated === 1 ? '' : 's'} left out`]])),
    }
  },
  describe({ data, fmt, name }) {
    const g = net(data)
    const used = g.nodes.filter((n) => n.inValue + n.outValue > 0)
    const sources = used.filter((n) => n.inValue === 0)
    const total = sources.reduce((s, n) => s + n.outValue, 0)
    const top = g.links.reduce<GLink | null>((a, l) => (!a || l.value > a.value ? l : a), null)
    const topText = top ? `, largest flow ${arrow(g.byId.get(top.source)!.label, g.byId.get(top.target)!.label)} at ${fmt.value(top.value)}` : ''
    return `${name}, ${phrase.count(used.length, 'node')} and ${phrase.count(g.links.length, 'flow')}, ${fmt.value(total)} in total from ${phrase.listPhrase(sources.map((n) => n.label))}${topText}`
  },
  table: (data) => linkTable(net(data)),
})

export default sankey
