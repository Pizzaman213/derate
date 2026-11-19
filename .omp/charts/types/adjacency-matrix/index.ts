// Adjacency matrix: every node is a row and a column; cell (a, b) is the link from a to b.
// n x n cells, so dense graphs that are a hairball as node-link diagrams stay readable.

import { defineChart, phrase, SIZES } from '../../core'
import type { BaseOptions, LegendItem, Mark } from '../../core'
import { notes } from '../../core/helpers'
import { graphFromMatrix, mapFields, toRows } from '../../adapters'
import { isRecord } from '../../adapters/util'
import { colorScale, extremesOf, hatchPath, inkOn } from '../_shared/MatrixCharts'
import { input, options, type AdjacencyOptions, type Graph } from './schema'

type Opts = AdjacencyOptions & BaseOptions

export interface AdjacencyModel {
  /** Node ids in display order. */
  ids: string[]
  label: Map<string, string>
  group: Map<string, string | undefined>
  groups: string[]
  /** value[i][j]: the link from ids[i] to ids[j]; undefined = no link, null = unknown weight. */
  value: Array<Array<number | null | undefined>>
  degree: Map<string, number>
  links: number
  duplicates: number
}

/** Link rows, CSV, column arrays, { names, matrix } or { nodes, links } -> { nodes, links }. */
function adapt(raw: unknown, o: Opts): unknown {
  const mapLinks = (rows: unknown) => {
    const r = toRows(rows)
    if (r === null) return rows
    return o.fields && Object.keys(o.fields).length ? mapFields(r, o.fields) : r
  }
  if (raw === null || raw === undefined) return { links: [] }
  if (isRecord(raw)) {
    if (Array.isArray(raw.names) && Array.isArray(raw.matrix)) return graphFromMatrix(raw.names.map(String), raw.matrix as number[][])
    if ('links' in raw) return { ...raw, links: mapLinks(raw.links) }
  }
  const rows = toRows(raw)
  return rows === null ? raw : { links: mapLinks(rows) }
}

export function model(g: Graph, o: Opts): AdjacencyModel {
  const ids: string[] = []
  const label = new Map<string, string>(), group = new Map<string, string | undefined>()
  const add = (id: string) => { if (!label.has(id)) { ids.push(id); label.set(id, id); group.set(id, undefined) } }
  for (const n of g.nodes) {
    add(n.id)
    if (n.label) label.set(n.id, n.label)
    if (n.group) group.set(n.id, n.group)
  }
  for (const l of g.links) { add(l.source); add(l.target) }
  const at = new Map(ids.map((id, i) => [id, i]))
  const n = ids.length
  const raw: Array<Array<number | null | undefined>> = ids.map(() => new Array<number | null | undefined>(n).fill(undefined))
  let duplicates = 0
  const put = (i: number, j: number, v: number | null) => {
    const row = raw[i]!
    const cur = row[j]
    row[j] = cur === undefined ? v : cur === null ? v : v === null ? cur : cur + v
  }
  for (const l of g.links) {
    const i = at.get(l.source)!, j = at.get(l.target)!
    if (raw[i]![j] !== undefined) duplicates++
    put(i, j, l.value)
  }
  let value = raw
  if (o.symmetric) {
    value = ids.map((_, i) => ids.map((__, j) => {
      const a = raw[i]![j], b = raw[j]![i]
      if (i === j || b === undefined) return a
      if (a === undefined) return b
      return a === null ? b : b === null ? a : a + b
    }))
  }
  const degree = new Map<string, number>()
  let links = 0
  for (let i = 0; i < n; i++) {
    const nb = new Set<number>()
    for (let j = 0; j < n; j++) {
      if (value[i]![j] !== undefined) { nb.add(j); if (!o.symmetric || j >= i) links++ }
      if (value[j]![i] !== undefined) nb.add(j)
    }
    degree.set(ids[i]!, nb.size)
  }
  const groups = [...new Set(ids.map((id) => group.get(id)).filter((x): x is string => x !== undefined))]
  const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' })
  const byName = (a: string, b: string) => collator.compare(label.get(a)!, label.get(b)!)
  const byDegree = (a: string, b: string) => degree.get(b)! - degree.get(a)! || byName(a, b)
  let order = ids
  if (o.order === 'name') order = [...ids].sort(byName)
  else if (o.order === 'degree') order = [...ids].sort(byDegree)
  else if (o.order === 'cluster') {
    const gi = (id: string) => { const x = group.get(id); return x === undefined ? groups.length : groups.indexOf(x) }
    order = [...ids].sort((a, b) => gi(a) - gi(b) || byDegree(a, b))
  }
  const perm = order.map((id) => at.get(id)!)
  return {
    ids: order, label, group, groups, degree, links, duplicates,
    value: perm.map((i) => perm.map((j) => value[i]![j])),
  }
}

const arrow = (o: Opts) => (o.symmetric ? '–' : '→')

export const adjacencyMatrix = defineChart<Graph, AdjacencyOptions>({
  id: 'adjacency-matrix',
  name: 'Adjacency matrix',
  category: 'matrix',
  intents: ['relationship', 'correlation', 'compare', 'network'],
  summary: 'Links between the members of one set as an n × n grid of cells.',
  input,
  options,
  shape: 'object',
  fields: ['source', 'target', 'value'],
  adapt,
  aspect: 0.85,
  minHeight: 220,
  maxHeight: 760,
  isEmpty: (g) => g.links.length === 0 && g.nodes.length === 0,
  draw(ctx) {
    const { theme, options: o, fmt } = ctx
    const m = model(ctx.data, o)
    const n = m.ids.length
    const lab = ((id: string) => m.label.get(id) ?? id) as (v: never) => string
    const pad = n > 30 ? 0 : 0.06
    const frame = (extra: { right?: number; bottom?: number }) => ctx.cartesian({
      x: { type: 'band', domain: m.ids, padding: pad },
      y: { type: 'band', domain: m.ids, padding: pad },
      xFormat: lab, yFormat: lab, grid: 'none', pad: extra,
    })
    // square cells: give the longer side's surplus back as padding, then lay out again
    let f = frame({})
    const surplus = f.plot.width - f.plot.height
    if (Math.abs(surplus) > 2) f = frame(surplus > 0 ? { right: surplus } : { bottom: -surplus })
    const marks: Mark[] = [...f.marks]
    const bw = Math.max(0.5, f.bw), bh = Math.max(0.5, f.bh)
    const vals = m.value.flat().filter((v): v is number => typeof v === 'number')
    // the weakest link must still stand apart from an empty cell
    const scale = colorScale(theme, vals, { diverging: 'auto', transform: o.colorScale, floor: 0.2 })
    const empty = theme.mix(theme.surface, theme.track, 0.55)
    const byGroup = o.color === 'group' && m.groups.length > 0
    const longest = Math.max(0, ...vals.map((v) => ctx.measure(fmt.value(v))))
    const printValues = o.showValues && longest + 6 <= bw && SIZES.tick + 4 <= bh
    const rx = Math.min(2, bw / 6)
    let nulls = 0
    m.ids.forEach((a, i) => m.ids.forEach((b, j) => {
      const v = m.value[i]![j]
      const x = f.px(b), y = f.py(a)
      const name = `${m.label.get(a)} ${arrow(o)} ${m.label.get(b)}`
      const datum = { source: a, target: b, value: v ?? null }
      const key = `${a}→${b}`
      if (v === undefined) {
        marks.push({ type: 'rect', role: 'cell', key, x, y, width: bw, height: bh, rx, fill: empty, datum, label: `${name}: no link`, tooltip: { title: name, rows: [['Link', 'none']] } })
        return
      }
      if (v === null) {
        nulls++
        marks.push({ type: 'rect', role: 'cell', key, x, y, width: bw, height: bh, rx, fill: theme.surface, stroke: theme.grid, strokeWidth: 1, datum, label: `${name}: weight unknown`, tooltip: { title: name, rows: [['Weight', 'unknown']] } })
        marks.push({ type: 'path', key: `hatch:${key}`, d: hatchPath(x, y, bw, bh, 4), fill: 'none', stroke: theme.muted, strokeWidth: 1, inert: true })
        return
      }
      let fill = scale.paint(v), fillOpacity: number | undefined, ink = inkOn(theme, scale.t(v), scale.diverging)
      if (byGroup) {
        const ga = m.group.get(a), gb = m.group.get(b)
        fill = ga !== undefined && ga === gb ? theme.cat(m.groups.indexOf(ga)) : theme.neutral
        fillOpacity = 0.3 + 0.7 * (scale.diverging ? Math.abs(scale.t(v) - 0.5) * 2 : scale.t(v))
        ink = theme.ink
      }
      marks.push({
        type: 'rect', role: 'cell', key, x, y, width: bw, height: bh, rx, fill, fillOpacity,
        datum, label: `${name}: ${fmt.value(v)}`, tooltip: { title: name, rows: [['Weight', fmt.value(v)]], color: fill },
      })
      if (printValues) marks.push({ type: 'text', layer: 'labels', key: `v:${key}`, x: x + bw / 2, y: y + bh / 2, text: fmt.value(v), anchor: 'middle', baseline: 'central', size: SIZES.tick, fill: ink, inert: true })
    }))
    // group boundaries in cluster order
    if (o.order === 'cluster' && m.groups.length > 1) {
      const step = n > 1 ? f.px(m.ids[1]!) - f.px(m.ids[0]!) : bw
      const gap = (step - bw) / 2
      for (let i = 1; i < n; i++) {
        if (m.group.get(m.ids[i]!) === m.group.get(m.ids[i - 1]!)) continue
        const px = f.px(m.ids[i]!) - gap, py = f.py(m.ids[i]!) - gap
        marks.push({ type: 'line', layer: 'labels', key: `sep-x:${m.ids[i]}`, x1: px, x2: px, y1: f.plot.y, y2: f.plot.y + f.plot.height, stroke: theme.ruleStrong, strokeWidth: 1, inert: true })
        marks.push({ type: 'line', layer: 'labels', key: `sep-y:${m.ids[i]}`, y1: py, y2: py, x1: f.plot.x, x2: f.plot.x + f.plot.width, stroke: theme.ruleStrong, strokeWidth: 1, inert: true })
      }
    }
    const items: LegendItem[] = byGroup ? [...m.groups.map((g, i) => ({ key: g, label: g, color: theme.cat(i), shape: 'square' as const })), { key: '(between groups)', label: 'Between groups', color: theme.neutral, shape: 'square' as const }] : []
    return {
      marks,
      plot: f.plot,
      legend: byGroup ? { items, toggle: false } : vals.length ? { gradient: scale.legend(fmt) } : undefined,
      note: notes([[m.duplicates, 'duplicate links summed'], [nulls, 'links of unknown weight hatched']]),
    }
  },
  describe({ data, options: o, fmt, name }) {
    const m = model(data, o)
    const strong = m.ids.flatMap((a, i) => m.ids.map((b, j) => ({ label: `${m.label.get(a)} ${arrow(o)} ${m.label.get(b)}`, value: m.value[i]![j] ?? null })))
      .filter((d) => d.value !== null)
    let hub = m.ids[0]!
    for (const id of m.ids) if (m.degree.get(id)! > m.degree.get(hub)!) hub = id
    const parts = [`${name}`, phrase.count(m.ids.length, 'node'), phrase.count(m.links, 'link')]
    if (strong.length) parts.push(extremesOf(strong, fmt, 'link').replace(/^highest /, 'strongest ').replace(', lowest ', ', weakest '))
    if (m.links) parts.push(`most connected ${m.label.get(hub)} (${phrase.count(m.degree.get(hub)!, 'neighbour')})`)
    return parts.join(', ')
  },
  table(data, o) {
    const m = model(data, o)
    const rows: Array<[string, string, number | null]> = []
    m.ids.forEach((a, i) => m.ids.forEach((b, j) => {
      const v = m.value[i]![j]
      if (v !== undefined && (!o.symmetric || j >= i)) rows.push([m.label.get(a)!, m.label.get(b)!, v])
    }))
    return { columns: [o.symmetric ? 'Node' : 'Source', o.symmetric ? 'Node' : 'Target', 'Weight'], rows }
  },
})

export default adjacencyMatrix
