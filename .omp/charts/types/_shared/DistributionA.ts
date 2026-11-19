// Shared by histogram, box-plot, violin, density and ridgeline: input adapting for
// plain number lists, grouping with null counting, tail collapsing, KDE bandwidths with
// a degenerate fallback, per-group statistics and path builders.

import { z } from 'zod'
import { AdapterError, mapFields, toRows } from '../../adapters'
import { stats, type Formatters, type Mark } from '../../core'

/**
 * Move rect/circle/line/text marks by (dx, dy) in their own geometry. Panels are placed
 * this way rather than with `transform`, because core/scene.ts lets a mark's transform
 * replace a text mark's rotation (rotated axis titles would come out upright).
 */
export function offsetMarks(marks: readonly Mark[], dx: number, dy: number, keyPrefix: string): Mark[] {
  return marks.map((m): Mark => {
    const key = `${keyPrefix}${m.key ?? m.type}`
    switch (m.type) {
      case 'rect': return { ...m, key, x: m.x + dx, y: m.y + dy }
      case 'circle': return { ...m, key, cx: m.cx + dx, cy: m.cy + dy }
      case 'line': return { ...m, key, x1: m.x1 + dx, x2: m.x2 + dx, y1: m.y1 + dy, y2: m.y2 + dy }
      case 'text': return { ...m, key, x: m.x + dx, y: m.y + dy }
      case 'path': return { ...m, key, transform: `translate(${dx},${dy})${m.transform ? ` ${m.transform}` : ''}` }
    }
  })
}

/** Group rows by `group`, pool the tail past maxGroups into "Other", then order by median. */
export function prepGroups(rows: ReadonlyArray<ValueRow & { group: string }>, sort: 'none' | 'ascending' | 'descending', maxGroups: number): { groups: Group[]; nulls: number; collapsed: number } {
  const g = groupValues(rows, (r) => r.group)
  const c = collapseTail(g.groups, maxGroups)
  return { groups: sortByMedian(c.groups, sort), nulls: g.nulls, collapsed: c.collapsed }
}

/** A row with an optional grouping key and a nullable value. */
export interface ValueRow {
  value: number | null
}

/** One group (or series) of finite values, in first-seen order. */
export interface Group {
  name: string
  values: number[]
  /** Position in first-seen order: stable colour index. */
  index: number
}

const shown = (v: unknown): string => {
  if (typeof v === 'string') return `'${v.length > 40 ? v.slice(0, 40) + '…' : v}'`
  if (typeof v === 'number' || typeof v === 'boolean') return String(v)
  if (Array.isArray(v)) return `a list of ${v.length}`
  return v === null ? 'null' : 'an object'
}

/**
 * Raw input -> rows for single-variable charts: a plain list of numbers becomes rows of
 * { value }; anything else goes through the usual rows adapters (CSV, column arrays,
 * field mapping). Used with shape 'object', because core rejects a number list before
 * a rows chart's adapt() would see it.
 */
export function adaptValueList(raw: unknown, fieldMap: Record<string, string> | undefined): unknown {
  if (raw === null || raw === undefined) return []
  if (Array.isArray(raw) && raw.length > 0 && raw.every((v) => v === null || typeof v === 'number' || typeof v === 'string')) {
    return raw.map((value) => ({ value }))
  }
  const rows = toRows(raw)
  if (rows === null) throw new AdapterError(`the data must be a list of numbers, a list of rows, CSV text, or column arrays, got ${shown(raw)}`)
  return fieldMap && Object.keys(fieldMap).length ? mapFields(rows, fieldMap) : rows
}

/** Group finite values by key (first-seen order); nulls are counted, not kept. */
export function groupValues<T extends ValueRow>(rows: readonly T[], keyOf: (r: T) => string): { groups: Group[]; nulls: number } {
  const map = new Map<string, Group>()
  let nulls = 0
  for (const r of rows) {
    const name = keyOf(r)
    let g = map.get(name)
    if (!g) {
      g = { name, values: [], index: map.size }
      map.set(name, g)
    }
    if (r.value === null || !Number.isFinite(r.value)) nulls++
    else g.values.push(r.value)
  }
  return { groups: [...map.values()].filter((g) => g.values.length > 0), nulls }
}

/** Keep the `max - 1` largest groups (original order) and pool the rest into "Other". */
export function collapseTail(groups: Group[], max: number): { groups: Group[]; collapsed: number } {
  if (groups.length <= max) return { groups, collapsed: 0 }
  const keep = new Set([...groups].sort((a, b) => b.values.length - a.values.length || a.index - b.index).slice(0, Math.max(1, max - 1)).map((g) => g.name))
  const kept = groups.filter((g) => keep.has(g.name))
  const rest = groups.filter((g) => !keep.has(g.name))
  let name = 'Other'
  while (keep.has(name)) name = `${name} `
  const other: Group = { name, values: rest.flatMap((g) => g.values), index: Math.max(...kept.map((g) => g.index)) + 1 }
  return { groups: [...kept, other], collapsed: rest.length }
}

/** Empty-state test shared by every chart here. */
export function emptyValues(rows: readonly ValueRow[]): boolean | string {
  if (rows.length === 0) return true
  return rows.some((r) => r.value !== null) ? false : 'All values are missing'
}

export const bandwidthOption = z
  .union([z.number().positive(), z.enum(['silverman', 'scott'])])
  .default('silverman')
  .describe("KDE bandwidth in data units, or a rule: 'silverman' | 'scott'")

const spread = (v: readonly number[]) => {
  let lo = Infinity, hi = -Infinity
  for (const x of v) { if (x < lo) lo = x; if (x > hi) hi = x }
  return v.length ? hi - lo : 0
}

/**
 * KDE bandwidth for one group. A rule on a degenerate group (one value, or all equal)
 * borrows the pooled data's bandwidth so a lone value draws as a bump the same width as
 * its neighbours; with nothing to borrow, core's fallback (10% of |value|, else 1) holds.
 */
export function bandwidthFor(values: readonly number[], spec: number | 'silverman' | 'scott', pooled: readonly number[] = values): number {
  if (typeof spec === 'number') return spec
  if (values.length > 1 && spread(values) > 0) return stats.bandwidth(values, spec)
  if (pooled.length > 1 && spread(pooled) > 0) return stats.bandwidth(pooled, spec)
  return stats.bandwidth(values, spec)
}

export type Pt = { x: number; y: number }

export const focusOption = z.enum(['auto', 'all']).default('auto').describe("'auto': when a few far outliers would squash the curves, the axis covers the bulk and the outliers are noted; 'all': always the full range")

/**
 * The bulk of the data when a few far outliers (beyond 3 IQR, at most 5% of values)
 * stretch the range past four times the bulk's own span; null when the full range reads fine.
 */
export function focusRange(values: readonly number[]): { range: [number, number]; hidden: number } | null {
  const b = stats.boxStats(values, { whisker: 3 })
  if (!b || !b.outliers.length || b.outliers.length > values.length * 0.05) return null
  const span = b.upperWhisker - b.lowerWhisker
  if (b.max - b.min <= 4 * span) return null
  return { range: [b.lowerWhisker, b.upperWhisker], hidden: b.outliers.length }
}

const INV_SQRT_2PI = 1 / Math.sqrt(2 * Math.PI)

/**
 * KDE on [lo, hi]. The grid is `points` even steps plus a fine local grid (h/4 apart,
 * out to the kernel's reach) around every occupied stretch of data, so a narrow cluster
 * next to a far outlier is still resolved -- an even grid of a few hundred points over a
 * range a thousand bandwidths wide would step straight over it.
 */
export function densityCurve(values: readonly number[], h: number, extent: [number, number], points = 160, kernel: 'gaussian' | 'epanechnikov' = 'gaussian'): Pt[] {
  const s = [...values].sort((a, b) => a - b)
  const n = s.length
  if (!n) return []
  const [lo, hi] = extent
  const reach = kernel === 'gaussian' ? 4 * h : h
  const xs: number[] = []
  for (let i = 0; i < points; i++) xs.push(i === points - 1 ? hi : lo + ((hi - lo) * i) / (points - 1))
  const step = h / 6
  // two cells past the reach, so every local grid ends on zero density before an even step
  const half = Math.ceil(reach / step) + 2
  const cells = new Set<number>()
  let last = NaN
  for (const v of s) {
    // values are sorted: one window per distinct cell
    const b = Math.round(v / step)
    if (b === last) continue
    last = b
    for (let j = -half; j <= half; j++) cells.add(b + j)
    if (cells.size > 20000) break
  }
  for (const c of cells) {
    const x = c * step
    if (x > lo && x < hi) xs.push(x)
  }
  xs.sort((a, b) => a - b)
  const k = kernel === 'gaussian' ? (u: number) => INV_SQRT_2PI * Math.exp(-0.5 * u * u) : (u: number) => (Math.abs(u) <= 1 ? 0.75 * (1 - u * u) : 0)
  const window = kernel === 'gaussian' ? 8 * h : h
  const out: Pt[] = []
  let start = 0
  let prev = -Infinity
  for (const x of xs) {
    if (x - prev < step / 8) {
      // near-duplicate: skip it, unless it is the extent's end, which replaces its neighbour
      if (x !== hi) continue
      out.pop()
    }
    prev = x
    while (start < n && s[start]! < x - window) start++
    let acc = 0
    for (let i = start; i < n && s[i]! <= x + window; i++) acc += k((x - s[i]!) / h)
    out.push({ x, y: acc / (n * h) })
  }
  return out
}

/** Trapezoid integral of a curve in data units. */
export function trapezoid(curve: readonly Pt[]): number {
  let s = 0
  for (let i = 1; i < curve.length; i++) s += ((curve[i]!.y + curve[i - 1]!.y) / 2) * (curve[i]!.x - curve[i - 1]!.x)
  return s
}

/** Five-number summary plus mean and sd; the table and the summaries read this. */
export interface GroupStats {
  name: string
  n: number
  min: number
  q1: number
  median: number
  q3: number
  max: number
  mean: number
  sd: number | null
}

export function groupStats(g: Group): GroupStats {
  const b = stats.boxStats(g.values)!
  const sd = g.values.length > 1 ? stats.stdev(g.values) : null
  return { name: g.name, n: b.n, min: b.min, q1: b.q1, median: b.median, q3: b.q3, max: b.max, mean: b.mean, sd }
}

/** Order groups by median. */
export function sortByMedian<T extends Group>(groups: T[], order: 'none' | 'ascending' | 'descending'): T[] {
  if (order === 'none') return groups
  const med = new Map(groups.map((g) => [g.name, stats.median(g.values)]))
  const dir = order === 'ascending' ? 1 : -1
  return [...groups].sort((a, b) => dir * (med.get(a.name)! - med.get(b.name)!))
}

const r2 = (v: number) => Math.round(v * 100) / 100

/** Polyline "M..L.." through points. */
export function lineD(pts: ReadonlyArray<readonly [number, number]>): string {
  return pts.map(([x, y], i) => `${i ? 'L' : 'M'}${r2(x)},${r2(y)}`).join('')
}

/** Closed polygon through points. */
export function polyD(pts: ReadonlyArray<readonly [number, number]>): string {
  return pts.length ? `${lineD(pts)}Z` : ''
}

/** Bounding box of points. */
export function boxOfPts(pts: ReadonlyArray<readonly [number, number]>): { x: number; y: number; width: number; height: number } {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity
  for (const [x, y] of pts) {
    if (x < x0) x0 = x
    if (x > x1) x1 = x
    if (y < y0) y0 = y
    if (y > y1) y1 = y
  }
  return { x: x0, y: y0, width: Math.max(1, x1 - x0), height: Math.max(1, y1 - y0) }
}

/** A density axis label: enough significant digits for small densities, locale-aware. */
export function densityFormat(fmt: Formatters, ticksMax: number): (v: number) => string {
  const digits = ticksMax > 0 ? Math.max(0, Math.min(8, 1 - Math.floor(Math.log10(ticksMax)))) : 2
  return (v: number) => fmt.num(v, digits)
}

/** "North, " or "" for the single unnamed series. */
export const prefix = (name: string) => (name ? `${name}, ` : '')

/** Sentence for one group's statistics. */
export function statsSentence(s: GroupStats, fmt: Formatters): string {
  return `median ${fmt.value(s.median)}, quartiles ${fmt.value(s.q1)} to ${fmt.value(s.q3)}, range ${fmt.value(s.min)} to ${fmt.value(s.max)} (n = ${fmt.integer(s.n)})`
}

/** Tooltip rows for one group's statistics. */
export function statsRows(s: GroupStats, fmt: Formatters): Array<[string, string]> {
  const rows: Array<[string, string]> = [
    ['n', fmt.integer(s.n)],
    ['Max', fmt.value(s.max)],
    ['Q3', fmt.value(s.q3)],
    ['Median', fmt.value(s.median)],
    ['Q1', fmt.value(s.q1)],
    ['Min', fmt.value(s.min)],
    ['Mean', fmt.value(s.mean)],
  ]
  if (s.sd !== null) rows.push(['SD', fmt.value(s.sd)])
  return rows
}

/** "highest median B at 48, lowest median E at 12" over group stats. */
export function medianExtremes(list: readonly GroupStats[], fmt: Formatters): string {
  if (!list.length) return 'no values'
  let hi = list[0]!, lo = list[0]!
  for (const s of list) {
    if (s.median > hi.median) hi = s
    if (s.median < lo.median) lo = s
  }
  if (list.length === 1) return `median ${fmt.value(hi.median)} (n = ${fmt.integer(hi.n)})`
  return `highest median ${hi.name} at ${fmt.value(hi.median)}, lowest median ${lo.name} at ${fmt.value(lo.median)}`
}

/** Table over group stats. */
export function statsTable(list: readonly GroupStats[], first = 'Group'): { columns: string[]; rows: Array<Array<string | number | null>> } {
  return {
    columns: [first, 'n', 'Min', 'Q1', 'Median', 'Q3', 'Max', 'Mean', 'SD'],
    rows: list.map((s) => [s.name, s.n, s.min, s.q1, s.median, s.q3, s.max, s.mean, s.sd]),
  }
}
