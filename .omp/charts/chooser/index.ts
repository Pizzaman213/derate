// recommend(intent, data) -> ranked [{ type, score, reason, options, data? }].
//
// The chooser reads the data's shape (chooser/inspect.ts), proposes every chart in the
// pack that can show it for the intent, reshapes the rows into that chart's input, and
// ranks. Guard rails, not taste: no pie/donut over 6 slices or with negatives, lines only
// over an ordered x. When nothing fits the intent it still returns the data's natural
// view, and says so.
//
// Only rules for registered charts belong here; the full set (geo, flow, hierarchy...)
// is in git under the `charts-full-pack` tag, with the 93 chart types it ranked.

import { getModule } from '../core/registry'
import type { Intent } from '../core/types'
import { inspect, type Column, type DataShape } from './inspect'

export { inspect }
export type { DataShape, Column }

export interface Recommendation {
  type: string
  score: number
  reason: string
  /** Options to render with (orientation, ...). */
  options: Record<string, unknown>
  /** The data reshaped into the chart's input. */
  data?: unknown
}
export interface ChooserOptions {
  /** Max recommendations returned (default 5). */
  limit?: number
}

type Row = Record<string, unknown>
const num = (v: unknown): number | null => {
  if (typeof v === 'number') return Number.isFinite(v) ? v : null
  if (typeof v === 'string' && v.trim() !== '') { const n = Number(v.replace(/,/g, '')); return Number.isFinite(n) ? n : null }
  return null
}
const MEASURE = /^(value|amount|revenue|sales|total|count|population|score|price|cost|spend|budget|qty|quantity|units|y|close)$/
const ID_LIKE = /^(id|key|code|iso\d?|iso_?\d|fips|zip|lat|latitude|lon|lng|longitude|year)$/
const LABEL = /^(name|label|category|product|region|country|state|city|item|group|team|department|segment|plan|question|host|owner|phase|title)$/

function measureOf(s: DataShape): Column | undefined {
  const cand = s.numeric.filter((c) => !ID_LIKE.test(c.name.toLowerCase()))
  return cand.find((c) => MEASURE.test(c.name.toLowerCase())) ?? cand[0]
}
function categoryOf(s: DataShape, max: number): Column | undefined {
  const cand = s.categorical.filter((c) => c.distinct >= 1 && c.distinct <= max)
  return cand.find((c) => LABEL.test(c.name.toLowerCase()) && !c.unique) ?? cand.find((c) => !c.unique) ?? cand.find((c) => LABEL.test(c.name.toLowerCase())) ?? cand[0]
}
function seriesOf(s: DataShape, skip: string[], max: number): Column | undefined {
  return s.categorical.filter((c) => !skip.includes(c.name) && c.distinct >= 2 && c.distinct <= max).sort((a, b) => a.distinct - b.distinct)[0]
}
/** Sum `measure` grouped by `keys`, renaming to canonical names (first-seen order); no measure counts rows. */
function sumBy(rows: Row[], keys: Array<[string, string]>, measure: [string, string] | null): Row[] {
  const out = new Map<string, Row>()
  for (const r of rows) {
    const k = keys.map(([src]) => String(r[src] ?? '')).join('\u0001')
    let o = out.get(k)
    if (!o) { o = Object.fromEntries(keys.map(([src, dst]) => [dst, r[src] ?? null])); if (measure) o[measure[1]] = null; out.set(k, o) }
    if (measure) {
      const v = num(r[measure[0]])
      if (v !== null) o[measure[1]] = ((o[measure[1]] as number | null) ?? 0) + v
    } else o.value = ((o.value as number | undefined) ?? 0) + 1
  }
  return [...out.values()]
}
/** One series for a single-measure bar or line chart: the measure's own name. */
const single = (rows: Row[], name: string): Row[] => rows.map((r) => ({ ...r, series: name }))
const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? '' : 's'}`

type Rule = (s: DataShape) => Recommendation[]

const rules: Partial<Record<Intent, Rule>> = {
  compare(s) {
    const m = measureOf(s)
    const cat = categoryOf(s, 60)
    const out: Recommendation[] = []
    if (m && cat) {
      const series = seriesOf(s, [cat.name], 6)
      if (series && cat.distinct <= 20) {
        const data = sumBy(s.rows, [[cat.name, 'label'], [series.name, 'series']], [m.name, 'value'])
        out.push({ type: 'grouped-bar', score: 86, reason: `${m.name} by ${cat.name} (${cat.distinct}) and ${series.name} (${series.distinct}): bars side by side compare within and across groups`, options: {}, data })
        out.push({ type: 'stacked-bar', score: 70, reason: `${m.name} by ${cat.name}, stacked by ${series.name}: totals first, parts second`, options: {}, data })
      } else {
        const long = cat.maxLen > 14 || cat.distinct > 12
        const data = single(sumBy(s.rows, [[cat.name, 'label']], [m.name, 'value']), m.name)
        out.push({ type: 'grouped-bar', score: 88, reason: `${m.name} summed by ${cat.name} (${plural(cat.distinct, 'category')})${long ? '; horizontal because the labels are long or many' : ''}`, options: long ? { orientation: 'horizontal' } : {}, data })
      }
    } else if (m && s.temporal[0]) {
      const t = s.temporal[0]
      const data = single(sumBy(s.rows, [[t.name, 'label']], [m.name, 'value']), m.name)
      if (data.length <= 36) out.push({ type: 'grouped-bar', score: 75, reason: `${m.name} per ${t.name} period (${data.length}): discrete periods compared as bars`, options: {}, data })
    }
    return out
  },
  rank(s) {
    const m = measureOf(s)
    const cat = categoryOf(s, 200)
    if (!m || !cat) return []
    const data = single(sumBy(s.rows, [[cat.name, 'label']], [m.name, 'value']), m.name).sort((a, b) => (num(b.value) ?? -Infinity) - (num(a.value) ?? -Infinity))
    return [{ type: 'grouped-bar', score: 90, reason: `${cat.name} ranked by ${m.name}, largest first`, options: { orientation: 'horizontal' }, data }]
  },
  composition(s) {
    const m = measureOf(s)
    const cat = categoryOf(s, 200)
    const out: Recommendation[] = []
    if (!m || !cat) return out
    const data = sumBy(s.rows, [[cat.name, 'label']], [m.name, 'value'])
    if (!data.some((d) => (num(d.value) ?? 0) < 0) && data.length <= 6) {
      out.push({ type: 'donut', score: 88, reason: `${data.length} parts of total ${m.name} by ${cat.name} — few enough slices to compare`, options: {}, data })
      out.push({ type: 'pie', score: 82, reason: `${data.length} parts of a whole`, options: {}, data })
    }
    const series = seriesOf(s, [cat.name], 8)
    if (series && cat.distinct <= 20) {
      const parts = sumBy(s.rows, [[cat.name, 'label'], [series.name, 'series']], [m.name, 'value'])
      out.push({ type: 'stacked-bar-100', score: 82, reason: `share of each ${series.name} within every ${cat.name}`, options: {}, data: parts })
      out.push({ type: 'stacked-bar', score: 76, reason: `each ${cat.name}'s total ${m.name}, split by ${series.name}`, options: {}, data: parts })
    }
    return out
  },
  trend(s) {
    const t = s.temporal[0]
    const m = measureOf(s)
    if (t && m) {
      const series = seriesOf(s, [], 10)
      if (series) return [{ type: 'multi-line', score: 90, reason: `${m.name} over ${t.name} (ordered time) for each of ${plural(series.distinct, series.name)}`, options: {}, data: sumBy(s.rows, [[t.name, 'x'], [series.name, 'series']], [m.name, 'y']) }]
      return [{ type: 'multi-line', score: 90, reason: `${m.name} over ${t.name}: a line for an ordered time axis`, options: {}, data: single(sumBy(s.rows, [[t.name, 'x']], [m.name, 'y']), m.name) }]
    }
    const x = m && s.numeric.find((c) => c.name !== m.name && c.ascending && c.distinct > 2)
    if (m && x) return [{ type: 'multi-line', score: 80, reason: `${m.name} along ordered ${x.name}`, options: {}, data: single(s.rows.map((r) => ({ x: r[x.name], y: r[m.name] })), m.name) }]
    return []
  },
  'change-over-time'(s) {
    return rules.trend!(s)
  },
}

/** The data's natural view, used when nothing matches the intent. */
function naturalView(s: DataShape): Recommendation[] {
  for (const i of (s.temporal.length ? ['trend', 'compare'] : ['compare']) as Intent[]) {
    const r = rules[i]!(s)
    if (r.length) return r
  }
  const cat = s.categorical[0]
  if (cat) return [{ type: 'grouped-bar', score: 50, reason: `rows counted by ${cat.name}`, options: {}, data: single(sumBy(s.rows, [[cat.name, 'label']], null), 'count') }]
  return []
}

/** Ranked chart recommendations for an intent and a dataset. */
export function recommend(intent: Intent, data: unknown, options: ChooserOptions = {}): Recommendation[] {
  const s = inspect(data)
  if (s.kind !== 'rows') return []
  let recs = (rules[intent]?.(s) ?? []).filter((r) => getModule(r.type))
  if (!recs.length) recs = naturalView(s).filter((r) => getModule(r.type)).map((r) => ({ ...r, score: Math.min(r.score, 40), reason: `no ${intent} structure in this data (${describe(s)}); its natural view instead: ${r.reason}` }))
  const seen = new Set<string>()
  return recs
    .sort((a, b) => b.score - a.score)
    .filter((r) => (seen.has(r.type) ? false : (seen.add(r.type), true)))
    .slice(0, options.limit ?? 5)
}

/** One line on what the data is. */
export function describe(s: DataShape): string {
  if (s.kind !== 'rows') return s.kind
  const parts = [`${s.count} rows`]
  if (s.temporal.length) parts.push(`time: ${s.temporal.map((c) => c.name).join(', ')}`)
  if (s.numeric.length) parts.push(`numbers: ${s.numeric.map((c) => c.name).join(', ')}`)
  if (s.categorical.length) parts.push(`categories: ${s.categorical.map((c) => `${c.name} (${c.distinct})`).join(', ')}`)
  return parts.join('; ')
}
