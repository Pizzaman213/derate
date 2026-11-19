// recommend(intent, data) -> ranked [{ type, score, reason, options, data? }].
//
// The chooser reads the data's shape (chooser/inspect.ts), proposes every chart that can
// show it for the intent, binds the data's columns to the chart's fields (through the
// `fields` option, or by reshaping when the chart needs aggregated input), and ranks.
// Guard rails, not taste: no pie/donut over 6 slices or with negatives, lines only over
// an ordered x, no dual axis unless asked (`dualAxis: true`), nothing 3-D (none exists).
// When nothing fits the intent it still returns the data's natural view, and says so.

import { getModule } from '../core/registry'
import type { Intent } from '../core/types'
import { inspect, type Column, type DataShape } from './inspect'

export { inspect }
export type { DataShape, Column }

export interface Recommendation {
  type: string
  score: number
  reason: string
  /** Options to render with (field mapping, sorting...). */
  options: Record<string, unknown>
  /** Present when the chart needs the data reshaped (aggregated, re-keyed). */
  data?: unknown
}
export interface ChooserOptions {
  /** Allow charts with a second value axis (only Pareto has one). */
  dualAxis?: boolean
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

function measureOf(s: DataShape, skip: string[] = []): Column | undefined {
  const cand = s.numeric.filter((c) => !skip.includes(c.name) && !ID_LIKE.test(c.name.toLowerCase()))
  return cand.find((c) => MEASURE.test(c.name.toLowerCase())) ?? cand[0]
}
function categoryOf(s: DataShape, skip: string[] = [], max = 60): Column | undefined {
  const cand = s.categorical.filter((c) => !skip.includes(c.name) && c.distinct >= 1 && c.distinct <= max)
  return cand.find((c) => LABEL.test(c.name.toLowerCase()) && !c.unique) ?? cand.find((c) => !c.unique) ?? cand.find((c) => LABEL.test(c.name.toLowerCase())) ?? cand[0]
}
function seriesOf(s: DataShape, skip: string[], max = 8): Column | undefined {
  return s.categorical.filter((c) => !skip.includes(c.name) && c.distinct >= 2 && c.distinct <= max).sort((a, b) => a.distinct - b.distinct)[0]
}
/** Sum `measure` grouped by `keys`, renaming to canonical names (first-seen order). */
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
const map = (pairs: Record<string, string | undefined>) => {
  const f: Record<string, string> = {}
  for (const [k, v] of Object.entries(pairs)) if (v && v !== k) f[k] = v
  return Object.keys(f).length ? { fields: f } : {}
}
const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? '' : 's'}`

type Rule = (s: DataShape, o: ChooserOptions) => Recommendation[]

// ---------------------------------------------------------------- rows
const rowsRules: Record<Intent, Rule> = {
  compare(s) {
    const m = measureOf(s)
    const cat = categoryOf(s, [], 60)
    const out: Recommendation[] = []
    if (m && cat) {
      const series = seriesOf(s, [cat.name], 6)
      if (series && cat.distinct <= 20) {
        const data = sumBy(s.rows, [[cat.name, 'label'], [series.name, 'series']], [m.name, 'value'])
        out.push({ type: 'grouped-bar', score: 86, reason: `${m.name} by ${cat.name} (${cat.distinct}) and ${series.name} (${series.distinct}): bars side by side compare within and across groups`, options: {}, data })
        out.push({ type: 'stacked-bar', score: 70, reason: `${m.name} by ${cat.name}, stacked by ${series.name}: totals first, parts second`, options: {}, data })
      }
      const data = sumBy(s.rows, [[cat.name, 'label']], [m.name, 'value'])
      const long = cat.maxLen > 14 || cat.distinct > 12
      out.push({ type: long ? 'horizontal-bar' : 'bar', score: 88, reason: `${m.name} summed by ${cat.name} (${plural(cat.distinct, 'category')})${long ? '; horizontal because the labels are long or many' : ''}`, options: {}, data })
      out.push({ type: long ? 'bar' : 'horizontal-bar', score: 72, reason: `same comparison, ${long ? 'vertical' : 'horizontal'} bars`, options: {}, data })
      out.push({ type: 'lollipop', score: 68, reason: `${m.name} by ${cat.name}: lighter ink than bars for many similar values`, options: {}, data })
      out.push({ type: 'dot-plot', score: 62, reason: `${m.name} by ${cat.name} on a non-zero axis`, options: {}, data })
    } else if (m && s.temporal[0]) {
      const t = s.temporal[0]
      const data = sumBy(s.rows, [[t.name, 'label']], [m.name, 'value'])
      if (data.length <= 36) out.push({ type: 'bar', score: 75, reason: `${m.name} per ${t.name} period (${data.length}): discrete periods compared as bars`, options: {}, data })
    }
    return out
  },
  rank(s) {
    const m = measureOf(s)
    const cat = categoryOf(s, [], 200)
    const out: Recommendation[] = []
    if (m && cat) {
      const data = sumBy(s.rows, [[cat.name, 'label']], [m.name, 'value'])
      out.push({ type: 'horizontal-bar', score: 90, reason: `${cat.name} ranked by ${m.name}, largest first`, options: { sort: 'descending' }, data })
      out.push({ type: 'lollipop', score: 74, reason: `ranking with less ink`, options: { sort: 'descending' }, data })
      const t = s.temporal[0]
      if (t) {
        const series = seriesOf(s, [], 12)
        if (series) out.push({ type: 'bump', score: 80, reason: `rank of each ${series.name} by ${m.name} over ${t.name}`, options: {}, data: sumBy(s.rows, [[t.name, 'x'], [series.name, 'series']], [m.name, 'value']) })
      }
    }
    return out
  },
  composition(s) {
    const out: Recommendation[] = []
    const m = measureOf(s)
    if (s.hierarchy && m) {
      out.push({ type: 'treemap', score: 90, reason: `parts of a hierarchy sized by ${m.name}`, options: map({ value: m.name }), data: s.rows })
      out.push({ type: 'sunburst', score: 80, reason: `the same hierarchy as rings`, options: map({ value: m.name }), data: s.rows })
    }
    const t = s.temporal[0]
    if (m && t) {
      const series = seriesOf(s, [], 10)
      if (series) {
        const data = sumBy(s.rows, [[t.name, 'x'], [series.name, 'series']], [m.name, 'y'])
        out.push({ type: 'stacked-area', score: 86, reason: `${m.name} over ${t.name}, split by ${series.name}: the total and its parts over time`, options: {}, data })
        out.push({ type: 'stacked-area-100', score: 80, reason: `each ${series.name}'s share of ${m.name} over time`, options: {}, data })
      }
    }
    const cat = categoryOf(s, [], 200)
    if (m && cat) {
      const data = sumBy(s.rows, [[cat.name, 'label']], [m.name, 'value'])
      const neg = data.some((d) => (num(d.value) ?? 0) < 0)
      if (!neg && data.length <= 6) {
        out.push({ type: 'donut', score: 88, reason: `${data.length} parts of total ${m.name} by ${cat.name} — few enough slices to compare`, options: {}, data })
        out.push({ type: 'pie', score: 82, reason: `${data.length} parts of a whole`, options: {}, data })
      }
      if (!neg) out.push({ type: 'treemap', score: data.length > 6 ? 86 : 70, reason: `${plural(data.length, 'part')} of total ${m.name}${data.length > 6 ? ' — too many slices for a pie' : ''}`, options: {}, data: data.map((d) => ({ label: d.label, value: d.value })) })
      const series = seriesOf(s, [cat.name], 8)
      if (series && cat.distinct <= 20) out.push({ type: 'stacked-bar-100', score: 82, reason: `share of each ${series.name} within every ${cat.name}`, options: {}, data: sumBy(s.rows, [[cat.name, 'label'], [series.name, 'series']], [m.name, 'value']) })
      if (!neg && data.length <= 10) out.push({ type: 'waffle', score: 64, reason: `parts of 100 squares`, options: {}, data })
    }
    return out
  },
  trend(s) {
    const out: Recommendation[] = []
    if (s.ohlc) {
      const o = s.ohlc
      out.push({ type: 'candlestick', score: 94, reason: `open/high/low/close over ${o.date}`, options: map({ date: o.date, open: o.open, high: o.high, low: o.low, close: o.close }), data: s.rows })
      out.push({ type: 'ohlc', score: 80, reason: `OHLC bars: open, high, low and close as ticks`, options: map({ date: o.date, open: o.open, high: o.high, low: o.low, close: o.close }), data: s.rows })
    }
    const t = s.temporal[0]
    const m = measureOf(s, s.ohlc ? [s.ohlc.open, s.ohlc.high, s.ohlc.low] : [])
    if (t && m) {
      const series = seriesOf(s, [], 10)
      if (series) {
        const data = sumBy(s.rows, [[t.name, 'x'], [series.name, 'series']], [m.name, 'y'])
        out.push({ type: 'multi-line', score: 90, reason: `${m.name} over ${t.name} (ordered time) for each of ${plural(series.distinct, series.name)}`, options: {}, data })
        out.push({ type: 'stacked-area', score: 66, reason: `the same series stacked to show the total`, options: {}, data })
      }
      const data = sumBy(s.rows, [[t.name, 'x']], [m.name, 'y'])
      out.push({ type: 'line', score: series ? 78 : 90, reason: `${m.name}${series ? ' (all series summed)' : ''} over ${t.name}: a line for an ordered time axis`, options: {}, data })
      out.push({ type: 'area', score: 70, reason: `${m.name} over time, filled to zero`, options: {}, data })
    } else if (m) {
      const x = s.numeric.find((c) => c.name !== m.name && c.ascending && c.distinct > 2)
      if (x) out.push({ type: 'line', score: 80, reason: `${m.name} along ordered ${x.name}`, options: map({ x: x.name, y: m.name }), data: s.rows })
    }
    return out
  },
  'change-over-time'(s, o) {
    const out = rowsRules.trend(s, o).map((r) => ({ ...r }))
    const t = s.temporal[0], m = measureOf(s), cat = categoryOf(s, [], 40)
    if (t && m && cat) {
      const periods = [...new Set(s.rows.map((r) => String(r[t.name])))].sort()
      if (periods.length >= 2) {
        const first = periods[0], last = periods[periods.length - 1]
        const by = new Map<string, { label: string; start: number | null; end: number | null }>()
        for (const r of s.rows) {
          const k = String(r[cat.name])
          const e = by.get(k) ?? { label: k, start: null, end: null }
          const v = num(r[m.name])
          if (String(r[t.name]) === first && v !== null) e.start = (e.start ?? 0) + v
          if (String(r[t.name]) === last && v !== null) e.end = (e.end ?? 0) + v
          by.set(k, e)
        }
        const data = [...by.values()].filter((d) => d.start !== null && d.end !== null)
        if (data.length) {
          out.push({ type: 'slope', score: 84, reason: `${m.name} for each ${cat.name}, first period vs last`, options: {}, data })
          out.push({ type: 'dumbbell', score: 74, reason: `start and end of each ${cat.name}'s change`, options: {}, data })
        }
      }
    }
    return out
  },
  timeline(s) {
    const out: Recommendation[] = []
    const name = categoryOf(s, [], 100000)
    if (s.interval && name) {
      const lane = seriesOf(s, [name.name], 12)
      const iv = s.interval
      out.push({ type: 'event-timeline', score: 86, reason: `${plural(s.count, 'item')} with ${iv.start}${iv.end ? `–${iv.end}` : ''} on one time axis`, options: {}, data: s.rows.map((r) => ({ date: r[iv.start], end: iv.end ? r[iv.end] : null, label: r[name.name], category: lane ? r[lane.name] : undefined })) })
      if (lane && iv.end) out.push({ type: 'swimlane-timeline', score: 84, reason: `items in a lane per ${lane.name}`, options: {}, data: s.rows.map((r) => ({ lane: r[lane.name], start: r[iv.start], end: r[iv.end!], label: r[name.name] })) })
    } else if (s.temporal[0] && name) {
      const t = s.temporal[0]
      if (s.count <= 200) out.push({ type: 'event-timeline', score: 80, reason: `${plural(s.count, 'event')} placed by ${t.name}`, options: {}, data: s.rows.map((r) => ({ date: r[t.name], label: r[name.name] })) })
    }
    return out
  },
  schedule(s, o) {
    const out: Recommendation[] = []
    const iv = s.interval
    const name = categoryOf(s, [], 100000)
    if (iv && name) {
      const id = s.columns.find((c) => /^(id|key|task_?id)$/i.test(c.name))
      const deps = s.columns.find((c) => /^(depends_?on|dependencies|deps|predecessors)$/i.test(c.name))
      const progress = s.numeric.find((c) => /^(progress|done|complete|percent)$/i.test(c.name))
      const milestone = s.columns.find((c) => /^milestone$/i.test(c.name))
      const group = s.categorical.find((c) => /^(phase|group|stream|team|epic)$/i.test(c.name))
      out.push({ type: 'gantt', score: 95, reason: `${plural(s.count, 'task')} with start${iv.end ? '/end' : ''}${deps ? ', dependencies' : ''}${progress ? ', progress' : ''}: a schedule`, options: map({ id: id?.name, name: name.name, start: iv.start, end: iv.end ?? undefined, duration: iv.duration ?? undefined, dependsOn: deps?.name, progress: progress?.name, milestone: milestone?.name, group: group?.name }), data: s.rows })
    }
    return [...out, ...rowsRules.timeline(s, o).map((r) => ({ ...r, score: r.score - 10 }))]
  },
  distribution(s) {
    const out: Recommendation[] = []
    const m = measureOf(s)
    if (!m) return out
    const group = categoryOf(s, [], 20)
    const values = s.rows.map((r) => ({ value: r[m.name] }))
    out.push({ type: 'histogram', score: group ? 80 : 90, reason: `how ${plural(s.count, 'value')} of ${m.name} spread`, options: {}, data: values })
    if (group && group.distinct >= 2) {
      const data = s.rows.map((r) => ({ group: r[group.name], value: r[m.name] }))
      out.push({ type: 'box-plot', score: 88, reason: `${m.name} spread per ${group.name} (${group.distinct} groups)`, options: {}, data })
      out.push({ type: 'violin', score: 78, reason: `distribution shapes per ${group.name}`, options: {}, data })
      out.push({ type: 'ridgeline', score: 70, reason: `stacked densities per ${group.name}`, options: {}, data })
    }
    out.push({ type: 'density', score: 72, reason: `smoothed density of ${m.name}`, options: {}, data: values })
    out.push({ type: 'ecdf', score: 60, reason: `cumulative share of ${m.name}`, options: {}, data: values })
    return out
  },
  relationship(s) {
    const out: Recommendation[] = []
    const nums = s.numeric.filter((c) => !/^(lat|latitude|lon|lng|longitude|id)$/i.test(c.name))
    if (nums.length >= 2) {
      const [x, y] = nums as [Column, Column]
      const series = seriesOf(s, [], 8)
      const label = categoryOf(s, [series?.name ?? ''], 100000)
      out.push({ type: 'scatter', score: 88, reason: `${y.name} against ${x.name} over ${plural(s.count, 'row')}${series ? `, coloured by ${series.name}` : ''}`, options: map({ x: x.name, y: y.name, series: series?.name, label: label?.name }), data: s.rows })
      const z = nums[2]
      if (z) out.push({ type: 'bubble', score: 82, reason: `${y.name} vs ${x.name}, sized by ${z.name}`, options: map({ x: x.name, y: y.name, size: z.name, series: series?.name, label: label?.name }), data: s.rows })
      out.push({ type: 'regression-scatter', score: 74, reason: `the same with a least-squares fit and R²`, options: map({ x: x.name, y: y.name }), data: s.rows })
    }
    return out
  },
  correlation(s) {
    const out: Recommendation[] = []
    const nums = s.numeric.filter((c) => !/^(lat|latitude|lon|lng|longitude|id)$/i.test(c.name))
    if (nums.length >= 3) {
      const cols = nums.slice(0, 8).map((c) => c.name)
      const data = s.rows.map((r) => Object.fromEntries(cols.map((c) => [c, num(r[c])])))
      out.push({ type: 'correlation-matrix', score: 90, reason: `pairwise correlation of ${plural(cols.length, 'numeric column')}`, options: {}, data })
      out.push({ type: 'splom', score: 78, reason: `every pair as a small scatter`, options: { columns: cols.slice(0, 5) }, data: s.rows })
    }
    if (nums.length >= 2) {
      const [x, y] = nums as [Column, Column]
      out.push({ type: 'regression-scatter', score: nums.length >= 3 ? 76 : 90, reason: `${y.name} vs ${x.name} with a least-squares fit and R²`, options: map({ x: x.name, y: y.name }), data: s.rows })
    }
    return out
  },
  flow(s) {
    const out: Recommendation[] = []
    if (s.links) {
      const l = s.links
      const data = s.rows.map((r) => ({ source: r[l.source], target: r[l.target], value: l.value ? r[l.value] : 1 }))
      out.push({ type: 'sankey', score: 90, reason: `flows from ${l.source} to ${l.target}`, options: {}, data })
      out.push({ type: 'chord', score: 74, reason: `flows between every pair`, options: {}, data })
    }
    const m = measureOf(s)
    const cats = s.categorical.filter((c) => c.distinct >= 2 && c.distinct <= 15)
    if (!s.links && cats.length >= 2) {
      const stages = cats.slice(0, 3).map((c) => c.name)
      const data = sumBy(s.rows, stages.map((c) => [c, c] as [string, string]), m ? [m.name, 'value'] : null)
      out.push({ type: 'alluvial', score: 82, reason: `${m?.name ?? 'rows'} flowing through ${stages.join(' → ')}`, options: { columns: stages }, data })
      const links = sumBy(s.rows, [[stages[0]!, 'source'], [stages[1]!, 'target']], m ? [m.name, 'value'] : null)
      out.push({ type: 'sankey', score: 76, reason: `${stages[0]} → ${stages[1]} flows`, options: {}, data: links })
    }
    return out
  },
  hierarchy(s) {
    const out: Recommendation[] = []
    const m = measureOf(s)
    if (s.hierarchy && 'id' in s.hierarchy) {
      const h = s.hierarchy
      const nameCol = s.categorical.find((c) => /^(name|label|title)$/i.test(c.name) && c.name !== h.id)
      const title = s.categorical.find((c) => /^(title|role|position)$/i.test(c.name) && c.name !== nameCol?.name)
      if (title) out.push({ type: 'org-chart', score: 92, reason: `${plural(s.count, 'person')} reporting through ${h.parent}`, options: map({ id: h.id, parent: h.parent, name: nameCol?.name, title: title.name }), data: s.rows })
      out.push({ type: 'tree', score: 86, reason: `${plural(s.count, 'node')} linked by ${h.parent}`, options: {}, data: s.rows.map((r) => ({ id: r[h.id], parent: r[h.parent] ?? null, name: nameCol ? r[nameCol.name] : r[h.id], value: m ? r[m.name] : undefined })) })
      if (m) out.push({ type: 'treemap', score: 84, reason: `the hierarchy sized by ${m.name}`, options: {}, data: s.rows.map((r) => ({ id: r[h.id], parent: r[h.parent] ?? null, label: nameCol ? r[nameCol.name] : r[h.id], value: r[m.name] })) })
    } else if (s.hierarchy && 'path' in s.hierarchy && m) {
      const p = s.hierarchy.path
      out.push({ type: 'treemap', score: 88, reason: `paths in ${p} sized by ${m.name}`, options: {}, data: s.rows.map((r) => ({ path: r[p], value: r[m.name] })) })
    } else {
      // two categorical levels that nest (every child under one parent) form a tree
      const cats = s.categorical.filter((c) => c.distinct >= 2).sort((a, b) => a.distinct - b.distinct)
      for (let i = 0; i < cats.length && !out.length; i++)
        for (let j = i + 1; j < cats.length && !out.length; j++) {
          const top = cats[i]!, sub = cats[j]!
          const parentOf = new Map<string, string>()
          const nests = s.rows.every((r) => { const k = String(r[sub.name]); const p = String(r[top.name]); if (parentOf.has(k) && parentOf.get(k) !== p) return false; parentOf.set(k, p); return true })
          if (!nests || !m) continue
          const data = sumBy(s.rows, [[top.name, 'top'], [sub.name, 'sub']], [m.name, 'value']).map((d) => ({ path: `${d.top}/${d.sub}`, value: d.value }))
          out.push({ type: 'treemap', score: 84, reason: `${sub.name} nested in ${top.name}, sized by ${m.name}`, options: {}, data })
          out.push({ type: 'sunburst', score: 78, reason: `${top.name} → ${sub.name} as rings`, options: {}, data })
        }
    }
    return out
  },
  network(s, o) {
    const out: Recommendation[] = []
    if (s.links) {
      const l = s.links
      const data = s.rows.map((r) => ({ source: r[l.source], target: r[l.target], value: l.value ? r[l.value] : 1 }))
      out.push({ type: 'force-network', score: 90, reason: `${plural(s.count, 'link')} between ${l.source} and ${l.target}`, options: {}, data })
      out.push({ type: 'arc-diagram', score: 74, reason: `the same links as arcs`, options: {}, data })
      out.push({ type: 'adjacency-matrix', score: 72, reason: `links as a matrix`, options: {}, data })
    } else if (s.hierarchy && 'id' in s.hierarchy) {
      const h = s.hierarchy
      const data = s.rows.filter((r) => r[h.parent] !== null && r[h.parent] !== undefined && r[h.parent] !== '').map((r) => ({ source: r[h.parent], target: r[h.id], value: 1 }))
      out.push({ type: 'force-network', score: 78, reason: `${h.parent} → ${h.id} links as a network`, options: {}, data })
      out.push(...rowsRules.hierarchy(s, o).map((r) => ({ ...r, score: r.score - 6 })))
    }
    return out
  },
  geo(s) {
    const out: Recommendation[] = []
    const m = measureOf(s)
    const g = s.geo
    if (g.region && g.map) {
      const data = sumBy(s.rows, [[g.region, 'region']], m ? [m.name, 'value'] : null)
      out.push({ type: 'choropleth', score: 92, reason: `${m?.name ?? 'count'} by ${g.region} on a ${g.map === 'world' ? 'world' : 'US states'} map`, options: { map: g.map }, data })
      if (g.map === 'us-states') out.push({ type: 'hex-tile-map', score: 82, reason: `equal-area US tiles, so small states read`, options: {}, data: data.map((d) => ({ state: d.region, value: d.value })) })
      if (!g.lat) out.push({ type: 'bubble-map', score: 76, reason: `proportional symbols at each ${g.region}`, options: { map: g.map }, data })
    }
    if (g.lat && g.lon) {
      const label = categoryOf(s, [], 100000)
      if (m) out.push({ type: 'bubble-map', score: g.region ? 84 : 90, reason: `${m.name} as circles at each latitude/longitude`, options: {}, data: s.rows.map((r) => ({ lon: r[g.lon!], lat: r[g.lat!], value: r[m.name], label: label ? r[label.name] : undefined })) })
      const cat = seriesOf(s, [], 8)
      out.push({ type: 'dot-map', score: 80, reason: `${plural(s.count, 'location')} as dots`, options: {}, data: s.rows.map((r) => ({ lon: r[g.lon!], lat: r[g.lat!], category: cat ? r[cat.name] : undefined })) })
    }
    return out
  },
  kpi(s) {
    const out: Recommendation[] = []
    const t = s.temporal[0], m = measureOf(s)
    if (t && m) {
      const series = sumBy(s.rows, [[t.name, 'x']], [m.name, 'y']).sort((a, b) => String(a.x).localeCompare(String(b.x)))
      const ys = series.map((d) => num(d.y)).filter((v): v is number => v !== null)
      if (ys.length) {
        out.push({ type: 'kpi-tile', score: 90, reason: `latest ${m.name} with the change since the previous ${t.name} and a sparkline`, options: {}, data: { label: m.name, value: ys[ys.length - 1], previous: ys.length > 1 ? ys[ys.length - 2] : undefined, series: ys } })
        out.push({ type: 'sparkline', score: 72, reason: `${m.name} as a word-sized trend`, options: {}, data: series })
      }
    }
    const target = s.numeric.find((c) => /^(target|goal|budget|plan|quota)$/i.test(c.name))
    const cat = categoryOf(s, [], 30)
    if (m && target && cat) {
      const data = sumBy(s.rows, [[cat.name, 'label']], [m.name, 'value']).map((d) => ({ ...d, target: s.rows.filter((r) => r[cat.name] === d.label).reduce((a, r) => a + (num(r[target.name]) ?? 0), 0) }))
      out.push({ type: 'bullet', score: 86, reason: `${m.name} against ${target.name} for each ${cat.name}`, options: {}, data })
    }
    return out
  },
  deviation(s) {
    const out: Recommendation[] = []
    const m = measureOf(s)
    const cat = categoryOf(s, [], 60)
    const target = s.numeric.find((c) => /^(target|goal|budget|plan|quota|baseline|expected)$/i.test(c.name))
    if (m && cat && target) {
      const agg = sumBy(s.rows, [[cat.name, 'label']], [m.name, 'value'])
      const tg = sumBy(s.rows, [[cat.name, 'label']], [target.name, 'value'])
      const data = agg.map((d, i) => ({ label: d.label, value: (num(d.value) ?? 0) - (num(tg[i]?.value) ?? 0) }))
      out.push({ type: 'diverging-bar', score: 90, reason: `${m.name} minus ${target.name} per ${cat.name}: above and below target`, options: {}, data })
      out.push({ type: 'bullet', score: 78, reason: `${m.name} against ${target.name}`, options: {}, data: agg.map((d, i) => ({ ...d, target: tg[i]?.value })) })
    } else if (m && cat && m.negatives > 0) {
      out.push({ type: 'diverging-bar', score: 88, reason: `${m.name} by ${cat.name} on both sides of zero`, options: {}, data: sumBy(s.rows, [[cat.name, 'label']], [m.name, 'value']) })
    }
    const t = s.temporal[0]
    if (m && t) out.push({ type: 'control-chart', score: target ? 74 : 86, reason: `${m.name} over ${t.name} against its mean and 3σ control limits`, options: {}, data: sumBy(s.rows, [[t.name, 'x']], [m.name, 'value']) })
    return out
  },
  sets(s) {
    if (!s.sets) return []
    const sc = s.sets
    const names = new Set<string>()
    for (const r of s.rows) for (const n of String(r[sc] ?? '').split(/[,;|]/).map((x) => x.trim()).filter(Boolean)) names.add(n)
    const out: Recommendation[] = [{ type: 'upset', score: names.size > 3 ? 92 : 80, reason: `${plural(names.size, 'set')}: intersection sizes as bars`, options: {}, data: s.rows }]
    if (names.size <= 3) out.unshift({ type: 'venn', score: 90, reason: `${plural(names.size, 'set')}: overlaps as circles`, options: {}, data: s.rows })
    return out
  },
}

// ---------------------------------------------------------------- object kinds
function objectRules(s: DataShape, intent: Intent): Recommendation[] {
  const raw = s.raw
  switch (s.kind) {
    case 'tree': {
      const all: Recommendation[] = [
        { type: 'treemap', score: 90, reason: 'a nested hierarchy with values: nested rectangles by size', options: {} },
        { type: 'sunburst', score: 84, reason: 'the hierarchy as rings', options: {} },
        { type: 'icicle', score: 76, reason: 'the hierarchy as stacked levels', options: {} },
        { type: 'circle-packing', score: 74, reason: 'the hierarchy as nested circles', options: {} },
        { type: 'tree', score: intent === 'hierarchy' || intent === 'network' ? 92 : 70, reason: 'the structure itself: who contains whom', options: {} },
      ]
      return all
    }
    case 'graph': {
      const g = raw as { links: Array<{ source: unknown; target: unknown }> }
      const flow = intent === 'flow'
      return [
        { type: flow ? 'sankey' : 'force-network', score: 90, reason: flow ? `${g.links.length} weighted links as flows` : `${g.links.length} links as a network`, options: {} },
        { type: flow ? 'chord' : 'arc-diagram', score: 76, reason: flow ? 'flows between every pair' : 'links as arcs along a line', options: {} },
        { type: 'adjacency-matrix', score: 72, reason: 'links as a matrix (no hairballs)', options: {} },
        ...(flow ? [] : [{ type: 'chord', score: 66, reason: 'links between groups as a circle', options: {} }]),
      ]
    }
    case 'sets': {
      const n = new Set((raw as { sets: Array<{ sets: string[] }> }).sets.flatMap((x) => x.sets)).size
      return n <= 3
        ? [{ type: 'venn', score: 92, reason: `${n} sets: overlaps as circles`, options: {} }, { type: 'upset', score: 80, reason: 'intersections as bars', options: {} }, { type: 'euler', score: 70, reason: 'area-proportional circles', options: {} }]
        : [{ type: 'upset', score: 92, reason: `${n} sets: too many for a Venn; intersections as bars`, options: {} }, { type: 'euler', score: 70, reason: 'area-proportional circles', options: {} }]
    }
    case 'kpi': {
      const k = raw as { max?: unknown; min?: unknown; target?: unknown }
      const bounded = k.max !== undefined
      return [
        { type: 'kpi-tile', score: 90, reason: 'one headline number with its change', options: {} },
        { type: bounded ? 'gauge' : 'progress-ring', score: bounded ? 84 : 72, reason: bounded ? 'a value within a known range' : 'progress towards 100', options: {} },
        { type: 'linear-progress', score: 70, reason: 'progress as a bar', options: {} },
      ]
    }
    case 'matrix':
      return [{ type: 'chord', score: 88, reason: 'a square matrix of flows between named groups', options: {} }]
    case 'numbers':
      return [
        { type: 'histogram', score: 90, reason: `${s.count} numbers: how they spread`, options: {}, data: (raw as unknown[]).map((v) => ({ value: v })) },
        { type: 'box-plot', score: 74, reason: 'quartiles and outliers', options: {}, data: (raw as unknown[]).map((v) => ({ group: 'all', value: v })) },
        { type: 'density', score: 72, reason: 'smoothed distribution', options: {}, data: (raw as unknown[]).map((v) => ({ value: v })) },
      ]
    case 'text':
      return [{ type: 'word-cloud', score: 85, reason: 'word frequencies in free text', options: {} }]
    default:
      return []
  }
}

/** The data's natural view, used when nothing matches the intent. */
function naturalView(s: DataShape, o: ChooserOptions): Recommendation[] {
  if (s.kind !== 'rows') return objectRules(s, 'compare')
  const order: Intent[] = s.ohlc ? ['trend'] : s.interval ? ['schedule', 'timeline'] : s.links ? ['network', 'flow'] : s.hierarchy ? ['hierarchy'] : s.geo.region || s.geo.lat ? ['geo'] : s.temporal.length ? ['trend', 'compare'] : s.categorical.length ? ['compare', 'distribution'] : ['distribution', 'relationship']
  for (const i of order) {
    const r = rowsRules[i](s, o)
    if (r.length) return r
  }
  const cat = s.categorical[0]
  if (cat) return [{ type: 'bar', score: 50, reason: `rows counted by ${cat.name}`, options: {}, data: sumBy(s.rows, [[cat.name, 'label']], null) }]
  return []
}

/** Ranked chart recommendations for an intent and a dataset. */
export function recommend(intent: Intent, data: unknown, options: ChooserOptions = {}): Recommendation[] {
  const s = inspect(data)
  if (s.kind === 'empty' || s.kind === 'unknown') return []
  let recs = s.kind === 'rows' ? rowsRules[intent](s, options) : objectRules(s, intent)
  // object kinds keep the original data
  recs = recs.map((r) => (r.data === undefined && s.kind !== 'rows' ? r : r.data === undefined ? { ...r, data: s.rows } : r))
  // guard rails
  recs = recs.filter((r) => getModule(r.type))
  recs = recs.filter((r) => options.dualAxis || r.type !== 'pareto')
  recs = recs.filter((r) => !['pie', 'donut', 'semi-donut'].includes(r.type) || (Array.isArray(r.data) && r.data.length <= 6))
  if (!recs.length) {
    const fallback = naturalView(s, options).filter((r) => getModule(r.type))
    recs = fallback.map((r) => ({ ...r, score: Math.min(r.score, 40), reason: `no ${intent} structure in this data (${describe(s)}); its natural view instead: ${r.reason}`, data: r.data === undefined && s.kind === 'rows' ? s.rows : r.data }))
  }
  const seen = new Set<string>()
  return recs
    .sort((a, b) => b.score - a.score)
    .filter((r) => (seen.has(r.type) ? false : (seen.add(r.type), true)))
    .slice(0, options.limit ?? 5)
}

/** One line on what the data is. */
export function describe(s: DataShape): string {
  if (s.kind !== 'rows') return `${s.kind}${s.count ? `, ${s.count} items` : ''}`
  const parts = [`${s.count} rows`]
  if (s.temporal.length) parts.push(`time: ${s.temporal.map((c) => c.name).join(', ')}`)
  if (s.numeric.length) parts.push(`numbers: ${s.numeric.map((c) => c.name).join(', ')}`)
  if (s.categorical.length) parts.push(`categories: ${s.categorical.map((c) => `${c.name} (${c.distinct})`).join(', ')}`)
  return parts.join('; ')
}
