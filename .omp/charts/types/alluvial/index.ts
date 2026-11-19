// Alluvial diagram: categorical stages in a fixed left-to-right order, a bar per category
// sized by its total, and bands between neighbouring stages. Bands keep the colour of
// their first-stage category all the way across, so a cohort can be followed.

import { defineChart, phrase, SIZES } from '../../core'
import { notes } from '../../core/helpers'
import type { BaseOptions, Mark } from '../../core'
import { AdapterError } from '../../adapters'
import { arrow, placeBoxes, textBox, type Box } from '../_shared/FlowA'
import { input, options, type AlluvialOptions, type AlluvialRow } from './schema'

const OTHER = 'Other'
const cell = (v: unknown) => (v === null || v === undefined || v === '' ? '(blank)' : String(v))

/** The stage columns: options.columns, else every column but value (and mapped sources) in first-seen order. */
function stageColumns(rows: ReadonlyArray<Record<string, unknown>>, o: Partial<AlluvialOptions> & BaseOptions): string[] {
  if (o.columns?.length) return o.columns
  const skip = new Set(['value', ...Object.values(o.fields ?? {})])
  const seen = new Set<string>()
  for (const r of rows) for (const k of Object.keys(r)) if (!skip.has(k)) seen.add(k)
  return [...seen]
}

interface Prepared {
  stages: string[]
  /** Distinct stage tuples with summed values (before "Other"). */
  tuples: Array<{ cats: string[]; value: number }>
  total: number
  dropped: { nulls: number; negatives: number; zeros: number; duplicates: number }
}

function prepare(data: AlluvialRow[], o: AlluvialOptions & BaseOptions): Prepared {
  const stages = stageColumns(data, o)
  const map = new Map<string, { cats: string[]; value: number }>()
  const dropped = { nulls: 0, negatives: 0, zeros: 0, duplicates: 0 }
  for (const r of data) {
    const v = r.value
    if (v === null) { dropped.nulls++; continue }
    if (v < 0) { dropped.negatives++; continue }
    if (v === 0) { dropped.zeros++; continue }
    const cats = stages.map((s) => cell(r[s]))
    const key = cats.join('\u0000')
    const hit = map.get(key)
    if (hit) { hit.value += v; dropped.duplicates++ }
    else map.set(key, { cats, value: v })
  }
  const tuples = [...map.values()]
  return { stages, tuples, total: tuples.reduce((s, t) => s + t.value, 0), dropped }
}

interface StageNode { stage: number; cat: string; value: number; y0: number; y1: number; order: number }
interface Flow { stage: number; origin: string; a: string; b: string; value: number; sy0: number; sy1: number; ty0: number; ty1: number }

export const alluvial = defineChart<AlluvialRow[], AlluvialOptions>({
  id: 'alluvial',
  name: 'Alluvial diagram',
  category: 'flow-hierarchy',
  intents: ['flow', 'composition'],
  summary: 'How one population divides across categorical stages, in a fixed column order.',
  input,
  options,
  shape: 'rows',
  fields: ['value'],
  numberFields: ['value'],
  aspect: 0.6,
  minHeight: 240,
  adapt(raw, o) {
    if (!Array.isArray(raw) || raw.length === 0) return raw
    const rows = raw as Array<Record<string, unknown>>
    const present = [...new Set(rows.flatMap((r) => Object.keys(r)))]
    const stages = stageColumns(rows, o)
    for (const s of stages) if (!present.includes(s)) throw new AdapterError(`columns: stage column '${s}' not found (columns: ${present.join(', ')})`)
    if (stages.length < 2) throw new AdapterError(`an alluvial diagram needs at least two stage columns, got ${stages.length ? `only '${stages[0]}'` : 'none'}; list them with options.columns`)
    return present.includes('value') ? rows : rows.map((r) => ({ ...r, value: 1 }))
  },
  isEmpty(data, o) {
    if (!data.length) return true
    const p = prepare(data, o)
    if (p.total > 0) return false
    return p.dropped.zeros > 0 && p.dropped.zeros + p.dropped.nulls === data.length ? 'All values are zero' : 'No positive values to draw'
  },
  draw(ctx) {
    const { theme, options: o, fmt, width: W, height: H } = ctx
    const p = prepare(ctx.data, o)
    const k = p.stages.length
    const size = SIZES.tick

    // categories per stage, with the tail grouped as "Other"
    let collapsed = 0
    const rename: Array<Map<string, string>> = []
    const orders: string[][] = []
    for (let s = 0; s < k; s++) {
      const totals = new Map<string, number>()
      for (const t of p.tuples) totals.set(t.cats[s]!, (totals.get(t.cats[s]!) ?? 0) + t.value)
      const firstSeen = [...totals.keys()]
      const bySize = [...firstSeen].sort((a, b) => totals.get(b)! - totals.get(a)!)
      const keep = new Set(firstSeen.length > o.maxCategories ? bySize.slice(0, o.maxCategories - 1) : firstSeen)
      const m = new Map(firstSeen.map((c) => [c, keep.has(c) ? c : OTHER]))
      collapsed += firstSeen.length - keep.size
      rename.push(m)
      const cats = [...new Set(m.values())].filter((c) => c !== OTHER || keep.has(OTHER))
      const sum = (c: string) => [...m].filter(([, to]) => to === c).reduce((acc, [from]) => acc + totals.get(from)!, 0)
      if (o.nodeSort === 'size') cats.sort((a, b) => sum(b) - sum(a))
      else if (o.nodeSort === 'name') cats.sort((a, b) => a.localeCompare(b))
      const other = firstSeen.length > keep.size
      orders.push(other ? [...cats.filter((c) => c !== OTHER), OTHER] : cats)
    }
    const tuples = p.tuples.map((t) => ({ cats: t.cats.map((c, s) => rename[s]!.get(c)!), value: t.value }))

    // vertical scale shared by every stage (each stage sums to the same total)
    const top = size + 10, bottom = 4
    const Hp = Math.max(20, H - top - bottom)
    const maxN = Math.max(...orders.map((c) => c.length))
    const gap = maxN > 1 ? Math.min(12, (Hp * 0.3) / (maxN - 1)) : 0
    const ky = (Hp - gap * (maxN - 1)) / p.total
    const nodeW = W < 500 ? 8 : 12
    const colX = (s: number) => (k > 1 ? (s * (W - nodeW)) / (k - 1) : 0)
    const nodes: StageNode[][] = orders.map((cats, s) => {
      const totals = new Map<string, number>()
      for (const t of tuples) totals.set(t.cats[s]!, (totals.get(t.cats[s]!) ?? 0) + t.value)
      const used = cats.reduce((acc, c) => acc + totals.get(c)! * ky, 0) + gap * (cats.length - 1)
      let y = top + (Hp - used) / 2
      return cats.map((cat, order) => {
        const v = totals.get(cat)!
        const node = { stage: s, cat, value: v, y0: y, y1: y + v * ky, order }
        y = node.y1 + gap
        return node
      })
    })
    const nodeAt = (s: number, cat: string) => nodes[s]!.find((n) => n.cat === cat)!

    // flows between neighbouring stages, keyed by (first-stage origin, left, right)
    const origins = orders[0]!
    const originIdx = new Map(origins.map((c, i) => [c, i]))
    const flows: Flow[] = []
    for (let s = 0; s < k - 1; s++) {
      const agg = new Map<string, Flow>()
      for (const t of tuples) {
        const key = `${t.cats[0]}\u0000${t.cats[s]}\u0000${t.cats[s + 1]}`
        const f = agg.get(key)
        if (f) f.value += t.value
        else agg.set(key, { stage: s, origin: t.cats[0]!, a: t.cats[s]!, b: t.cats[s + 1]!, value: t.value, sy0: 0, sy1: 0, ty0: 0, ty1: 0 })
      }
      const list = [...agg.values()]
      const ord = (st: number, c: string) => nodeAt(st, c).order
      const outCursor = new Map<string, number>(), inCursor = new Map<string, number>()
      for (const f of [...list].sort((x, y) => ord(s, x.a) - ord(s, y.a) || ord(s + 1, x.b) - ord(s + 1, y.b) || originIdx.get(x.origin)! - originIdx.get(y.origin)!)) {
        const y = outCursor.get(f.a) ?? nodeAt(s, f.a).y0
        f.sy0 = y
        f.sy1 = y + f.value * ky
        outCursor.set(f.a, f.sy1)
      }
      for (const f of [...list].sort((x, y) => ord(s + 1, x.b) - ord(s + 1, y.b) || ord(s, x.a) - ord(s, y.a) || originIdx.get(x.origin)! - originIdx.get(y.origin)!)) {
        const y = inCursor.get(f.b) ?? nodeAt(s + 1, f.b).y0
        f.ty0 = y
        f.ty1 = y + f.value * ky
        inCursor.set(f.b, f.ty1)
      }
      flows.push(...list)
    }

    // a first-stage "Other" made by grouping is a remainder, not a cohort: neutral grey
    const otherIsRemainder = [...rename[0]!].some(([from, to]) => to === OTHER && from !== OTHER)
    const color = (origin: string) => (origin === OTHER && otherIsRemainder ? theme.neutral : theme.cat(originIdx.get(origin)!))
    const marks: Mark[] = []
    for (const f of flows) {
      const x0 = colX(f.stage) + nodeW, x1 = colX(f.stage + 1), xm = (x0 + x1) / 2
      const d = `M${x0},${f.sy0}C${xm},${f.sy0} ${xm},${f.ty0} ${x1},${f.ty0}L${x1},${f.ty1}C${xm},${f.ty1} ${xm},${f.sy1} ${x0},${f.sy1}Z`
      const name = arrow(f.a, f.b)
      const fromNode = nodeAt(f.stage, f.a)
      const sentence = f.stage === 0 ? name : `${name} (${p.stages[0]}: ${f.origin})`
      marks.push({
        type: 'path', role: 'flow', key: `flow:${f.stage}:${f.origin}:${f.a}:${f.b}`, d, fill: color(f.origin), fillOpacity: 0.5,
        cx: xm, cy: (f.sy0 + f.sy1 + f.ty0 + f.ty1) / 4, box: { x: x0, y: Math.min(f.sy0, f.ty0), width: Math.max(1, x1 - x0), height: Math.max(f.sy1, f.ty1) - Math.min(f.sy0, f.ty0) },
        series: f.origin, pattern: originIdx.get(f.origin)! % 8,
        datum: { stage: f.stage, from: p.stages[f.stage], to: p.stages[f.stage + 1], origin: f.origin, source: f.a, target: f.b, value: f.value },
        label: `${sentence}: ${fmt.value(f.value)}`,
        tooltip: {
          title: sentence,
          rows: [['Value', fmt.value(f.value)], [`Share of ${f.a}`, fmt.pct(f.value / fromNode.value)]],
          color: color(f.origin),
        },
      })
    }
    for (const col of nodes)
      for (const n of col) {
        const fill = n.stage === 0 ? color(n.cat) : theme.ink
        marks.push({
          type: 'rect', role: 'node', key: `node:${n.stage}:${n.cat}`, x: colX(n.stage), y: n.y0, width: nodeW, height: Math.max(1, n.y1 - n.y0), rx: 1,
          fill, series: n.stage === 0 ? n.cat : undefined, pattern: n.stage === 0 ? originIdx.get(n.cat)! % 8 : undefined,
          datum: { stage: p.stages[n.stage], category: n.cat, value: n.value },
          label: `${p.stages[n.stage]} ${n.cat}: ${fmt.value(n.value)} (${fmt.pct(n.value / p.total)})`,
          tooltip: { title: `${p.stages[n.stage]}: ${n.cat}`, rows: [['Total', fmt.value(n.value)], ['Share', fmt.pct(n.value / p.total)]], color: fill },
        })
      }

    // stage headers: first left-aligned, last right-aligned, the rest centred on their column
    const spacing = k > 1 ? (W - nodeW) / (k - 1) : W
    p.stages.forEach((st, s) => {
      const anchor = s === 0 ? 'start' : s === k - 1 ? 'end' : 'middle'
      const x = s === 0 ? 0 : s === k - 1 ? W : colX(s) + nodeW / 2
      const max = s === 0 || s === k - 1 ? spacing / 2 - 6 : spacing - 12
      const text = ctx.truncate(st, max, size)
      if (!text) return
      marks.push({ type: 'text', layer: 'labels', key: `stage:${s}`, x, y: size, text, full: text === st ? undefined : st, anchor, baseline: 'central', fill: theme.inkSecondary, size, weight: 600 })
    })

    // category labels beside their bar, inside the gap toward the neighbouring stage
    const minLabel = ctx.measure('Ab…', size)
    type Opt = { box: Box; text: string; x: number; y: number; anchor: 'start' | 'end' }
    const flat = nodes.flat()
    const opts: Opt[][] = flat.map((n) => {
      const cy = Math.min(H - size * 0.7, Math.max(top + size * 0.6, (n.y0 + n.y1) / 2))
      const value = o.showValues ? ` ${fmt.value(n.value)}` : ''
      const valueW = value ? ctx.measure(value, size) : 0
      const room = spacing - nodeW - 12
      const right = { anchor: 'start' as const, avail: n.stage === k - 1 ? 0 : room, x: colX(n.stage) + nodeW + 5 }
      const left = { anchor: 'end' as const, avail: n.stage === 0 ? 0 : room, x: colX(n.stage) - 5 }
      const out: Opt[] = []
      for (const sd of n.stage === k - 1 ? [left] : [right, left])
        for (const frac of [1, 0.5]) {
          const avail = sd.avail * frac - valueW
          if (avail < minLabel) continue
          const text = ctx.truncate(n.cat, avail, size) + value
          out.push({ text, x: sd.x, y: cy, anchor: sd.anchor, box: textBox(sd.x, cy, ctx.measure(text, size), sd.anchor, size) })
        }
      return out
    })
    const order = flat.map((_, i) => i).sort((a, b) => flat[b]!.value - flat[a]!.value)
    const blockers = flat.map((n) => ({ x: colX(n.stage), y: n.y0, width: nodeW, height: n.y1 - n.y0 }))
    const pick = placeBoxes(opts.map((os) => os.map((x) => x.box)), order, 1, blockers)
    flat.forEach((n, i) => {
      const op = opts[i]![pick[i]!]
      if (!op) return
      const truncated = !op.text.startsWith(n.cat)
      marks.push({ type: 'text', layer: 'labels', key: `label:${n.stage}:${n.cat}`, x: op.x, y: op.y, text: op.text, full: truncated ? n.cat : undefined, anchor: op.anchor, baseline: 'central', fill: theme.ink, size, halo: true, inert: !truncated })
    })

    const d = p.dropped
    return {
      marks,
      legend: origins.length > 1 ? { title: p.stages[0], items: origins.map((c, i) => ({ key: c, label: c, color: color(c), shape: 'square' as const, pattern: i % 8 })) } : undefined,
      note: notes([
        [d.negatives, `negative value${d.negatives === 1 ? '' : 's'} omitted`],
        [d.nulls, `row${d.nulls === 1 ? '' : 's'} without a value left out`],
        [d.duplicates, `duplicate row${d.duplicates === 1 ? '' : 's'} summed`],
        [collapsed, `small categor${collapsed === 1 ? 'y' : 'ies'} grouped as "${OTHER}"`],
      ]),
    }
  },
  describe({ data, options: o, fmt, name }) {
    const p = prepare(data, o)
    let best: { stage: string; cat: string; value: number } | null = null
    p.stages.forEach((st, s) => {
      const totals = new Map<string, number>()
      for (const t of p.tuples) totals.set(t.cats[s]!, (totals.get(t.cats[s]!) ?? 0) + t.value)
      for (const [cat, value] of totals) if (!best || value > best.value) best = { stage: st, cat, value }
    })
    const b = best as { stage: string; cat: string; value: number } | null
    const top = b ? `, largest ${b.cat} in ${b.stage} at ${fmt.value(b.value)} (${fmt.pct(b.value / p.total)})` : ''
    return `${name}, ${phrase.count(p.stages.length, 'stage')} (${phrase.listPhrase(p.stages, 5)}), ${fmt.value(p.total)} in total${top}`
  },
  table(data, o) {
    const p = prepare(data, o)
    return { columns: [...p.stages, 'Value'], rows: p.tuples.map((t) => [...t.cats, t.value]) }
  },
})

export default alluvial
