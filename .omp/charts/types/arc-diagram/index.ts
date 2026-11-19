// Arc diagram: nodes in a row along a baseline, every link an arc above it (thickness =
// weight, height = distance), node size = number of links. Labels hang below the
// baseline, rotated, thinned where they would collide.

import { defineChart, phrase, SIZES } from '../../core'
import { notes } from '../../core/helpers'
import type { Mark } from '../../core'
import { adaptGraph, netNotes, nodeColors, nodeLinkTable, normalizeNet, placeBoxes, type GNode, type Net } from '../_shared/FlowA'
import { input, options, type ArcInput, type ArcOptions } from './schema'

const net = (data: ArcInput): Net => normalizeNet(data, { undirected: true })

function ordered(g: Net, order: ArcOptions['order']): GNode[] {
  if (order === 'degree') return [...g.nodes].sort((a, b) => b.degree - a.degree || b.inValue + b.outValue - (a.inValue + a.outValue) || a.index - b.index)
  if (order === 'group') {
    const gi = new Map(g.groups.map((x, i) => [x, i]))
    const rank = (n: GNode) => (n.group === undefined ? g.groups.length : gi.get(n.group)!)
    return [...g.nodes].sort((a, b) => rank(a) - rank(b) || a.index - b.index)
  }
  return g.nodes
}

export const arcDiagram = defineChart<ArcInput, ArcOptions>({
  id: 'arc-diagram',
  name: 'Arc diagram',
  category: 'flow-hierarchy',
  intents: ['network', 'relationship'],
  summary: 'Nodes along a line with every connection drawn as an arc above it.',
  input,
  options,
  shape: 'object',
  adapt: (raw, o) => adaptGraph(raw, o),
  aspect: 0.55,
  minHeight: 200,
  isEmpty: (data) => net(data).nodes.length === 0,
  draw(ctx) {
    const { theme, options: o, fmt, width: W, height: H } = ctx
    const g = net(ctx.data)
    const nodes = ordered(g, o.order)
    const n = nodes.length
    const size = SIZES.tick
    const { color, pattern, legend } = nodeColors(g, theme, 'single')
    const grouped = g.groups.length > 0

    const widest = Math.max(...nodes.map((d) => ctx.measure(d.label, size)))
    const labelRoom = Math.min(widest, H * 0.3, 140) + 8
    const pad = Math.max(10, size)
    const step = n > 1 ? (W - 2 * pad) / (n - 1) : 0
    const rMax = Math.max(2, Math.min(8, n > 1 ? step * 0.45 : 8))
    const rMin = Math.min(2.5, rMax)
    const baseY = H - labelRoom - rMax - 4
    const x = new Map(nodes.map((d, i) => [d.id, n > 1 ? pad + i * step : W / 2]))
    const maxDeg = Math.max(1, ...nodes.map((d) => d.degree))
    const radius = (d: GNode) => rMin + (rMax - rMin) * Math.sqrt(d.degree / maxDeg)

    const vmax = Math.max(0, ...g.links.map((l) => l.value))
    const wMax = Math.max(1.5, Math.min(8, step * 0.6))
    const spanMax = Math.max(1, ...g.links.map((l) => Math.abs(x.get(l.target)! - x.get(l.source)!) / 2))
    const squash = Math.min(1, Math.max(0.05, (baseY - rMax - wMax / 2 - 4) / spanMax))
    const marks: Mark[] = []
    for (const d of nodes) {
      const c = color(d)
      const links = `${d.degree} link${d.degree === 1 ? '' : 's'}`
      marks.push({
        type: 'circle', role: 'node', key: `node:${d.id}`, cx: x.get(d.id)!, cy: baseY, r: radius(d),
        fill: c, stroke: theme.surface, strokeWidth: 1, pattern: pattern(d), series: grouped ? d.group : undefined,
        datum: d, label: `${d.label}: ${links}${g.weighted ? `, total weight ${fmt.value(d.inValue + d.outValue)}` : ''}`,
        tooltip: { title: d.group ? `${d.label} (${d.group})` : d.label, rows: [['Links', String(d.degree)], ...(g.weighted ? [['Total weight', fmt.value(d.inValue + d.outValue)] as [string, string]] : [])], color: c },
      })
    }
    // arcs leave from the top of each node so they never paint over one; longest first so short ones sit on top
    const links = [...g.links].sort((a, b) => Math.abs(x.get(b.target)! - x.get(b.source)!) - Math.abs(x.get(a.target)! - x.get(a.source)!))
    for (const l of links) {
      const s = g.byId.get(l.source)!, t = g.byId.get(l.target)!
      const [left, right] = x.get(s.id)! <= x.get(t.id)! ? [s, t] : [t, s]
      const x1 = x.get(left.id)!, x2 = x.get(right.id)!
      const y1 = baseY - radius(left), y2 = baseY - radius(right)
      const rx = (x2 - x1) / 2, ry = rx * squash
      const w = g.weighted && vmax > 0 ? 1 + (wMax - 1) * Math.sqrt(l.value / vmax) : 1.25
      const name = `${s.label} ↔ ${t.label}`
      const top = Math.min(y1, y2) - ry
      marks.push({
        type: 'path', role: 'link', key: `link:${l.key}`, d: `M${x1},${y1}A${rx},${Math.max(0.01, ry)} 0 0 1 ${x2},${y2}`,
        fill: 'none', stroke: color(s), strokeWidth: w, strokeOpacity: 0.55, linecap: 'round',
        cx: (x1 + x2) / 2, cy: top, box: { x: x1, y: top - w / 2, width: Math.max(2, x2 - x1), height: Math.max(2, Math.max(y1, y2) - top + w / 2) },
        series: grouped ? s.group : undefined,
        datum: l, label: g.weighted ? `${name}: ${fmt.value(l.value)}` : name,
        tooltip: { title: name, rows: g.weighted ? [['Weight', fmt.value(l.value)]] : [], color: color(s) },
      })
    }

    // rotated labels below the line: the most connected win where they would collide
    const ly = baseY + rMax + 4
    const room = H - ly - 2
    const texts = nodes.map((d) => ctx.truncate(d.label, room, size))
    const boxes = nodes.map((d, i) => [{ x: x.get(d.id)! - size * 0.6, y: ly, width: texts[i] ? size * 1.2 : 0, height: ctx.measure(texts[i]!, size) }])
    const order = nodes.map((_, i) => i).sort((a, b) => nodes[b]!.degree - nodes[a]!.degree || a - b)
    const pick = placeBoxes(boxes, order, 0.5)
    nodes.forEach((d, i) => {
      if (pick[i]! < 0) return
      const text = texts[i]!
      const truncated = text !== d.label
      marks.push({ type: 'text', layer: 'labels', key: `label:${d.id}`, x: x.get(d.id)!, y: ly, text, full: truncated ? d.label : undefined, anchor: 'end', baseline: 'central', rotate: -90, fill: theme.ink, size, inert: !truncated })
    })
    return { marks, legend, note: notes(netNotes(g.dropped)) }
  },
  describe({ data, name }) {
    const g = net(data)
    const top = g.nodes.reduce((a, b) => (b.degree > a.degree ? b : a), g.nodes[0]!)
    const most = top && top.degree > 0 ? `, most connected ${top.label} (${phrase.count(top.degree, 'link')})` : ', no links'
    const groups = g.groups.length > 1 ? `, ${phrase.count(g.groups.length, 'group')}` : ''
    return `${name}, ${phrase.count(g.nodes.length, 'node')} and ${phrase.count(g.links.length, 'link')}${groups}${most}`
  },
  table: (data) => nodeLinkTable(net(data)),
})

export default arcDiagram
