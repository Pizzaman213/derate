// Histogram: values binned into contiguous ranges; bar height = count (or share, or
// density). Several series share the same bin edges and either overlay
// semi-transparently or get one panel each with shared scales.

import { cartesian, defineChart, phrase, SIZES } from '../../core'
import type { Formatters, Mark, Rect } from '../../core'
import { binValues } from '../../adapters'
import { notes } from '../../core/helpers'
import { adaptValueList, emptyValues, groupValues, offsetMarks, prefix, type Group } from '../_shared/DistributionA'
import { input, options, type HistogramOptions, type HistogramRow } from './schema'

export interface SeriesBins {
  name: string
  index: number
  n: number
  counts: number[]
}

export interface Binned {
  /** Bin edges, length bins + 1, shared by every series. */
  edges: number[]
  series: SeriesBins[]
  nulls: number
  /** Values outside a fixed bins.domain. */
  outside: number
}

/** Bin every series on one set of edges (pure; the tests read it). */
export function binSeries(data: readonly HistogramRow[], o: Pick<HistogramOptions, 'bins'>): Binned {
  const { groups, nulls } = groupValues(data, (r) => r.series ?? '')
  const all = groups.flatMap((g) => g.values)
  if (!all.length) return { edges: [], series: [], nulls, outside: 0 }
  let lo = Infinity, hi = -Infinity
  for (const v of all) { if (v < lo) lo = v; if (v > hi) hi = v }
  let edges: number[]
  const dom = o.bins.domain ? ([Math.min(...o.bins.domain), Math.max(...o.bins.domain)] as [number, number]) : undefined
  if (!dom && lo === hi) {
    // all equal: one bin centred on the value
    const d = Math.abs(lo) * 0.05 || 0.5
    edges = [lo - d, lo + d]
  } else {
    const bins = binValues(all, { count: o.bins.count, thresholds: o.bins.thresholds, method: o.bins.method, domain: dom })
    edges = bins.length ? [bins[0]!.x0, ...bins.map((b) => b.x1)] : dom ? [dom[0], dom[1]] : [lo, hi]
  }
  const domain: [number, number] = [edges[0]!, edges[edges.length - 1]!]
  let outside = 0
  const series = groups.map((g: Group) => {
    const inner = g.values.filter((v) => v >= domain[0] && v <= domain[1])
    outside += g.values.length - inner.length
    const bins = binValues(inner, { domain, thresholds: edges.slice(1, -1) })
    const counts = edges.slice(1).map((_, i) => bins[i]?.count ?? 0)
    return { name: g.name, index: g.index, n: inner.length, counts }
  })
  return { edges, series, nulls, outside }
}

/** Bar height for a bin under the normalisation. */
function heightOf(count: number, n: number, width: number, mode: HistogramOptions['normalize']): number {
  if (mode === 'count') return count
  if (n === 0) return 0
  return mode === 'percent' ? count / n : count / (n * width)
}

function range(fmt: Formatters, a: number, b: number): string {
  return `${fmt.value(a)}–${fmt.value(b)}`
}

export const histogram = defineChart<HistogramRow[], HistogramOptions>({
  id: 'histogram',
  name: 'Histogram',
  category: 'distribution',
  intents: ['distribution'],
  summary: 'Counts of values falling into contiguous ranges (bins).',
  input,
  options,
  shape: 'object',
  fields: ['value', 'series'],
  numberFields: ['value'],
  adapt: (raw, o) => adaptValueList(raw, o.fields),
  isEmpty: (data) => emptyValues(data),
  draw(ctx) {
    const { theme, options: o, fmt } = ctx
    const b = binSeries(ctx.data, o)
    const { edges } = b
    const multi = b.series.length > 1
    const visible = b.series.filter((s) => !ctx.hidden.has(s.name))
    const yOf = (s: SeriesBins, i: number) => heightOf(s.counts[i]!, s.n, edges[i + 1]! - edges[i]!, o.normalize)
    let ymax = 0
    for (const s of visible) s.counts.forEach((_, i) => { ymax = Math.max(ymax, yOf(s, i)) })
    if (ymax === 0) ymax = o.normalize === 'count' ? 1 : 0.01
    const yFormat = o.normalize === 'percent' ? (v: number) => fmt.pct(v) : o.normalize === 'density' ? (v: number) => fmt.num(v, Math.max(0, Math.min(8, 1 - Math.floor(Math.log10(ymax))))) : (v: number) => fmt.integer(v)
    const yTitle = o.yTitle ?? (o.normalize === 'count' ? 'Count' : o.normalize === 'percent' ? 'Share of values' : 'Density')
    const overlay = multi && o.layout === 'overlay'

    // a lone bin fills a fifth of the axis rather than all of it
    const pad = edges.length === 2 ? 2 * (edges[1]! - edges[0]!) : 0
    const xDom: [number, number] = [edges[0]! - pad, edges[edges.length - 1]! + pad]

    const panel = (rect: Rect, list: SeriesBins[], keyPre: string, showXTitle: boolean, showYTitle: boolean): Mark[] => {
      const f = cartesian(
        { width: rect.width, height: rect.height, fmt, theme, measure: ctx.measure, truncate: ctx.truncate, zoom: null, zoomAxes: undefined },
        {
          x: { type: 'linear', domain: xDom, zero: false, nice: false },
          y: { type: 'linear', domain: [0, ymax], zero: true },
          yFormat,
          yTickCount: rect.height < 160 ? 3 : undefined,
          xTitle: showXTitle ? o.xTitle : undefined,
          yTitle: showYTitle ? yTitle : undefined,
        },
      )
      const out: Mark[] = [...f.marks]
      const y0 = f.py(0)
      for (const s of list) {
        const color = theme.cat(s.index)
        s.counts.forEach((count, i) => {
          if (count === 0) return
          const a = edges[i]!, z = edges[i + 1]!
          const xa = f.px(a), xz = f.px(z)
          const w = xz - xa
          const gap = w > 4 ? 1 : 0
          const v = yOf(s, i)
          const yv = f.py(v)
          const share = s.n ? count / s.n : 0
          const what = `${prefix(s.name)}${range(fmt, a, z)}: ${phrase.count(count, 'value')}`
          const rows: Array<[string, string]> = [['Range', range(fmt, a, z)], ['Count', fmt.integer(count)], ['Share', fmt.pct(share, 1)]]
          if (o.normalize === 'density') rows.push(['Density', yFormat(v)])
          out.push({
            type: 'rect', role: 'bar', key: `${keyPre}${s.name}:${i}`, series: multi ? s.name : undefined,
            x: xa + gap / 2, width: Math.max(0.5, w - gap), y: Math.min(yv, y0 - 1), height: Math.max(1, y0 - yv),
            fill: color, fillOpacity: overlay ? 0.45 : 1, stroke: overlay ? color : undefined, strokeWidth: overlay ? 1 : undefined,
            pattern: multi ? s.index : undefined,
            datum: { series: s.name || undefined, x0: a, x1: z, count, share },
            label: `${what} (${fmt.pct(share, 1)})`,
            tooltip: { title: s.name || range(fmt, a, z), rows, color },
          })
        })
      }
      return rect.x || rect.y ? offsetMarks(out, rect.x, rect.y, keyPre) : out
    }

    const marks: Mark[] = []
    if (!multi || overlay) marks.push(...panel({ x: 0, y: 0, width: ctx.width, height: ctx.height }, visible, '', true, true))
    else {
      const list = visible.length ? visible : b.series.slice(0, 1)
      // the column count that keeps panels closest to a readable 200 x 110 minimum
      const th = SIZES.label + 10
      let cols = 1
      let best = -Infinity
      for (let c = 1; c <= list.length; c++) {
        const r = Math.ceil(list.length / c)
        const score = Math.min(ctx.width / c / 200, (ctx.height / r - th) / 110)
        if (score > best + 1e-9) { best = score; cols = c }
      }
      const rowsN = Math.ceil(list.length / cols)
      const pw = ctx.width / cols, ph = Math.max(40, (ctx.height - rowsN * th) / rowsN)
      list.forEach((s, k) => {
        const c = k % cols, r = Math.floor(k / cols)
        const x = c * pw, y = r * (ph + th)
        const title = ctx.truncate(s.name, pw - 16, SIZES.label)
        marks.push({ type: 'text', layer: 'labels', key: `ft:${s.name}`, x: x + 8, y: y + th - 6, text: title, full: title !== s.name ? s.name : undefined, size: SIZES.label, fill: theme.ink, baseline: 'alphabetic' })
        marks.push(...panel({ x, y: y + th, width: pw, height: ph }, [s], `f${s.index}:`, r === rowsN - 1, c === 0))
      })
    }
    return {
      marks,
      legend: multi ? { items: b.series.map((s) => ({ key: s.name, label: s.name, color: theme.cat(s.index), pattern: s.index })) } : undefined,
      note: notes([[b.nulls, b.nulls === 1 ? 'missing value dropped' : 'missing values dropped'], [b.outside, `value${b.outside === 1 ? '' : 's'} outside the bin range left out`]]),
    }
  },
  describe({ data, options: o, fmt, name }) {
    const b = binSeries(data, o)
    const n = b.series.reduce((t, s) => t + s.n, 0)
    const bins = b.edges.length - 1
    const totals = b.edges.slice(1).map((_, i) => b.series.reduce((t, s) => t + s.counts[i]!, 0))
    let top = 0
    totals.forEach((c, i) => { if (c > totals[top]!) top = i })
    const lead = b.series.length > 1 ? `${phrase.count(b.series.length, 'series', 'series')}, ` : ''
    return `${name}, ${lead}${phrase.count(n, 'value')} in ${phrase.count(bins, 'bin')} from ${fmt.value(b.edges[0]!)} to ${fmt.value(b.edges[bins]!)}, most in ${range(fmt, b.edges[top]!, b.edges[top + 1]!)} (${fmt.integer(totals[top]!)})`
  },
  table(data, o) {
    const b = binSeries(data, o)
    const multi = b.series.length > 1
    const rows: Array<Array<string | number | null>> = []
    for (const s of b.series) s.counts.forEach((c, i) => rows.push([...(multi ? [s.name] : []), b.edges[i]!, b.edges[i + 1]!, c]))
    return { columns: [...(multi ? ['Series'] : []), 'From', 'To', 'Count'], rows }
  },
})

export default histogram
