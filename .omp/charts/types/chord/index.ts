// Chord diagram: groups around a circle (arc length = everything the group sends), and
// ribbons between pairs whose ends are each direction's flow. Paths are built in
// absolute coordinates (no transform) so the canvas fallback paints them in place.

import { chord as d3chord, type Chord, type ChordGroup } from 'd3-chord'
import { descending, ascending } from 'd3-array'
import { path as d3path } from 'd3-path'
import { defineChart, phrase, SIZES } from '../../core'
import { notes } from '../../core/helpers'
import type { Mark, Rect } from '../../core'
import { isRecord } from '../../adapters'
import { adaptGraph, arrow, parseGraph, placeBoxes, textBox, type Box } from '../_shared/FlowA'
import { input, options, type ChordInput, type ChordOptions } from './schema'

const HALF_PI = Math.PI / 2

interface Clean {
  names: string[]
  matrix: number[][]
  total: number
  dropped: { nulls: number; negatives: number; duplicates: number }
}

const cleanCache = new WeakMap<ChordInput, Clean>()
/** Nulls and negatives -> 0 (counted); rows/columns sharing a name are merged (counted). */
function clean(data: ChordInput): Clean {
  const hit = cleanCache.get(data)
  if (hit) return hit
  const index = new Map<string, number>()
  const names: string[] = []
  const at = data.names.map((n) => {
    let i = index.get(n)
    if (i === undefined) { i = names.length; index.set(n, i); names.push(n) }
    return i
  })
  const matrix = names.map(() => names.map(() => 0))
  const dropped = { nulls: 0, negatives: 0, duplicates: data.names.length - names.length }
  let total = 0
  data.matrix.forEach((row, i) =>
    row.forEach((v, j) => {
      if (v === null) { dropped.nulls++; return }
      if (v < 0) { dropped.negatives++; return }
      matrix[at[i]!]![at[j]!]! += v
      total += v
    }))
  const out = { names, matrix, total, dropped }
  cleanCache.set(data, out)
  return out
}

/** A point on a circle at a chord angle (0 = 12 o'clock, clockwise). */
const polar = (cx: number, cy: number, r: number, a: number): [number, number] => [cx + r * Math.sin(a), cy - r * Math.cos(a)]

function annulus(cx: number, cy: number, r0: number, r1: number, a0: number, a1: number): string {
  const p = d3path()
  const [x0, y0] = polar(cx, cy, r1, a0)
  p.moveTo(x0, y0)
  p.arc(cx, cy, r1, a0 - HALF_PI, a1 - HALF_PI)
  const [x1, y1] = polar(cx, cy, r0, a1)
  p.lineTo(x1, y1)
  p.arc(cx, cy, r0, a1 - HALF_PI, a0 - HALF_PI, true)
  p.closePath()
  return p.toString()
}

/** d3-chord's ribbon, drawn around (cx, cy). */
function ribbon(cx: number, cy: number, r: number, s: ChordGroup | Chord['source'], t: Chord['target']): string {
  const p = d3path()
  const [sx, sy] = polar(cx, cy, r, s.startAngle)
  p.moveTo(sx, sy)
  p.arc(cx, cy, r, s.startAngle - HALF_PI, s.endAngle - HALF_PI)
  if (s.startAngle !== t.startAngle || s.endAngle !== t.endAngle) {
    const [tx, ty] = polar(cx, cy, r, t.startAngle)
    p.quadraticCurveTo(cx, cy, tx, ty)
    p.arc(cx, cy, r, t.startAngle - HALF_PI, t.endAngle - HALF_PI)
  }
  p.quadraticCurveTo(cx, cy, sx, sy)
  p.closePath()
  return p.toString()
}

const bounds = (pts: Array<[number, number]>): Rect => {
  const xs = pts.map((q) => q[0]), ys = pts.map((q) => q[1])
  const x = Math.min(...xs), y = Math.min(...ys)
  return { x, y, width: Math.max(2, Math.max(...xs) - x), height: Math.max(2, Math.max(...ys) - y) }
}

export const chord = defineChart<ChordInput, ChordOptions>({
  id: 'chord',
  name: 'Chord diagram',
  category: 'flow-hierarchy',
  intents: ['flow', 'relationship'],
  summary: 'Flows between groups around a circle: arc length is what a group sends, ribbons join each pair.',
  input,
  options,
  shape: 'object',
  aspect: 0.75,
  minHeight: 240,
  maxHeight: 720,
  adapt(raw, o) {
    if (isRecord(raw) && Array.isArray(raw.names) && Array.isArray(raw.matrix)) return raw
    const g = parseGraph(adaptGraph(raw, o))
    const ids: string[] = []
    const index = new Map<string, number>()
    const label = new Map((g.nodes ?? []).map((n) => [n.id, n.label ?? n.id]))
    const add = (id: string) => {
      if (!index.has(id)) { index.set(id, ids.length); ids.push(id) }
      return index.get(id)!
    }
    for (const n of g.nodes ?? []) add(n.id)
    for (const l of g.links) { add(l.source); add(l.target) }
    const weighted = g.links.some((l) => l.value !== null)
    const matrix: Array<Array<number | null>> = ids.map(() => ids.map(() => 0))
    for (const l of g.links) {
      const row = matrix[index.get(l.source)!]!, j = index.get(l.target)!
      const v = weighted ? l.value : 1
      const cur = row[j]!
      if (v !== null) row[j] = (cur ?? 0) + v
      else if (cur === 0) row[j] = null
    }
    return { names: ids.map((id) => label.get(id) ?? id), matrix }
  },
  isEmpty(data) {
    if (!data.names.length) return true
    const c = clean(data)
    return c.total > 0 ? false : 'All values are zero'
  },
  draw(ctx) {
    const { theme, options: o, fmt, width: W, height: H } = ctx
    const c = clean(ctx.data)
    const n = c.names.length
    const size = SIZES.tick
    const rowSum = c.matrix.map((r) => r.reduce((a, v) => a + v, 0))
    const colSum = c.names.map((_, j) => c.matrix.reduce((a, r) => a + r[j]!, 0))
    const live = c.names.filter((_, i) => rowSum[i]! > 0)
    const widest = Math.max(0, ...live.map((nm) => ctx.measure(nm, size)))
    const labelRoom = Math.min(widest, W * 0.2) + 10
    const R = Math.max(16, Math.min(H / 2 - size - 8, W / 2 - labelRoom - 4))
    const thick = Math.max(5, Math.min(14, R * 0.07))
    const inner = R - thick
    const cx = W / 2, cy = H / 2
    const pad = o.padAngle ?? (live.length > 1 ? Math.min(0.04, (2 * Math.PI * 0.25) / live.length) : 0)
    const layout = d3chord().padAngle(pad).sortSubgroups(descending).sortChords(descending)
    if (o.sort !== 'input') layout.sortGroups(o.sort === 'descending' ? descending : ascending)
    const chords = layout(c.matrix)
    const color = (i: number) => theme.cat(i)
    const marks: Mark[] = []

    for (const ch of chords) {
      const s = ch.source, t = ch.target
      if (s.value <= 0 && t.value <= 0) continue
      const a = c.names[s.index]!, b = c.names[t.index]!
      const self = s.index === t.index
      const fwd = c.matrix[s.index]![t.index]!, back = c.matrix[t.index]![s.index]!
      const sp = polar(cx, cy, inner, (s.startAngle + s.endAngle) / 2), tp = polar(cx, cy, inner, (t.startAngle + t.endAngle) / 2)
      const mid: [number, number] = self ? polar(cx, cy, inner * 0.85, (s.startAngle + s.endAngle) / 2) : [cx + (sp[0] - cx + tp[0] - cx) / 4, cy + (sp[1] - cy + tp[1] - cy) / 4]
      const label = self ? `${arrow(a, a)}: ${fmt.value(fwd)}` : back > 0 ? `${arrow(a, b)}: ${fmt.value(fwd)}; ${arrow(b, a)}: ${fmt.value(back)}` : `${arrow(a, b)}: ${fmt.value(fwd)}`
      marks.push({
        type: 'path', role: 'ribbon', key: `ribbon:${a}→${b}`, d: ribbon(cx, cy, inner - 1, s, t),
        fill: color(s.index), fillOpacity: 0.62, stroke: chords.length <= 300 ? theme.surface : undefined, strokeWidth: chords.length <= 300 ? 0.5 : undefined, pattern: s.index % 8,
        cx: mid[0], cy: mid[1], box: bounds([sp, tp, mid]),
        datum: { source: a, target: b, value: fwd, reverse: self ? null : back }, label,
        tooltip: {
          title: self ? `Within ${a}` : `${a} ↔ ${b}`,
          rows: self ? [['Value', fmt.value(fwd)]] : [[arrow(a, b), fmt.value(fwd)], [arrow(b, a), fmt.value(back)]],
          color: color(s.index),
        },
      })
    }

    const groups = chords.groups.filter((g) => g.value > 0)
    for (const g of groups) {
      const nm = c.names[g.index]!
      const midA = (g.startAngle + g.endAngle) / 2
      const pts: Array<[number, number]> = []
      for (let k = 0; k <= 8; k++) {
        const a = g.startAngle + ((g.endAngle - g.startAngle) * k) / 8
        pts.push(polar(cx, cy, R, a), polar(cx, cy, inner, a))
      }
      const [mx, my] = polar(cx, cy, (R + inner) / 2, midA)
      marks.push({
        type: 'path', role: 'arc', key: `group:${nm}`, d: annulus(cx, cy, inner, R, g.startAngle, g.endAngle),
        fill: color(g.index), pattern: g.index % 8, cx: mx, cy: my, box: bounds(pts),
        datum: { name: nm, out: rowSum[g.index], in: colSum[g.index] },
        label: `${nm}: ${fmt.value(rowSum[g.index]!)} out, ${fmt.value(colSum[g.index]!)} in`,
        tooltip: { title: nm, rows: [['Out', fmt.value(rowSum[g.index]!)], ['In', fmt.value(colSum[g.index]!)], ['Share of all flows', fmt.pct(rowSum[g.index]! / c.total)]], color: color(g.index) },
      })
    }

    // labels around the outside, horizontal, truncated to the room toward the edge
    const minLabel = ctx.measure('Ab…', size)
    type Opt = { box: Box; text: string; x: number; y: number; anchor: 'start' | 'middle' | 'end' }
    const opts: Opt[][] = groups.map((g) => {
      const nm = c.names[g.index]!
      const a = (g.startAngle + g.endAngle) / 2
      const [x, y] = polar(cx, cy, R + 6, a)
      const sin = Math.sin(a)
      const anchor = sin > 0.2 ? 'start' : sin < -0.2 ? 'end' : 'middle'
      const avail = anchor === 'start' ? W - x - 2 : anchor === 'end' ? x - 2 : 2 * Math.min(x, W - x) - 4
      if (avail < minLabel) return []
      const text = ctx.truncate(nm, Math.min(avail, labelRoom + R), size)
      const yy = Math.min(H - size * 0.7, Math.max(size * 0.7, y + (anchor === 'middle' ? -Math.cos(a) * size * 0.3 : 0)))
      return [{ text, x, y: yy, anchor, box: textBox(x, yy, ctx.measure(text, size), anchor, size) }]
    })
    const order = groups.map((_, i) => i).sort((p, q) => groups[q]!.value - groups[p]!.value)
    const pick = placeBoxes(opts.map((os) => os.map((x) => x.box)), order, 1)
    groups.forEach((g, i) => {
      const op = opts[i]![pick[i]!]
      if (!op) return
      const nm = c.names[g.index]!
      const truncated = op.text !== nm
      marks.push({ type: 'text', layer: 'labels', key: `label:${nm}`, x: op.x, y: op.y, text: op.text, full: truncated ? nm : undefined, anchor: op.anchor, baseline: 'central', fill: theme.ink, size, inert: !truncated })
    })

    const d = c.dropped
    const silent = n - groups.length
    return {
      marks,
      note: notes([
        [d.negatives, `negative value${d.negatives === 1 ? '' : 's'} omitted`],
        [d.nulls, `missing value${d.nulls === 1 ? '' : 's'} left out`],
        [d.duplicates, `duplicate name${d.duplicates === 1 ? '' : 's'} merged`],
        [silent, `group${silent === 1 ? '' : 's'} with nothing to send left out`],
      ]),
    }
  },
  describe({ data, fmt, name }) {
    const c = clean(data)
    const rows = c.names.map((nm, i) => ({ label: nm, value: c.matrix[i]!.reduce((a, v) => a + v, 0) }))
    let top: { a: string; b: string; v: number } | null = null
    let flows = 0
    c.matrix.forEach((r, i) => r.forEach((v, j) => {
      if (v <= 0) return
      flows++
      if (!top || v > top.v) top = { a: c.names[i]!, b: c.names[j]!, v }
    }))
    const best = rows.reduce((p, q) => (q.value > p.value ? q : p), rows[0]!)
    const t = top as { a: string; b: string; v: number } | null
    return `${name}, ${phrase.count(c.names.length, 'group')} and ${phrase.count(flows, 'flow')}, largest sender ${best.label} at ${fmt.value(best.value)}${t ? `, largest flow ${arrow(t.a, t.b)} at ${fmt.value(t.v)}` : ''}`
  },
  table(data) {
    const c = clean(data)
    const rows: Array<Array<string | number | null>> = []
    c.matrix.forEach((r, i) => r.forEach((v, j) => { if (v > 0) rows.push([c.names[i]!, c.names[j]!, v]) }))
    return { columns: ['Source', 'Target', 'Value'], rows }
  },
})

export default chord
