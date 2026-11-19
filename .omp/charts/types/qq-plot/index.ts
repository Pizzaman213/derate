// Normal Q-Q plot: each sorted observation against the normal quantile of its plotting
// position (stats.qqPoints), with the reference line through the quartiles (stats.qqLine).
// Points on the line mean "normal"; the way the tails bend away names the departure.

import { defineChart, phrase, stats, type Formatters, type Mark } from '../../core'
import { finiteExtent, notes } from '../../core/helpers'
import { autoRadius, finiteValues, padExtent, spread, type Indexed } from '../_shared/DistributionB'
import { input, options, type QqOptions, type QqRow } from './schema'

interface QqPoint { d: Indexed<QqRow>; theoretical: number; sample: number; rank: number }
interface Model { points: QqPoint[]; line: { slope: number; intercept: number } | null; r: number; nulls: number; mean: number; sd: number }

/** Sorted observations paired with their normal quantiles, the quartile line, and the plot's correlation. */
function model(rows: QqRow[], o: QqOptions): Model {
  const { ok, nulls } = finiteValues(rows)
  const values = ok.map((d) => d.value)
  const mean = stats.mean(values)
  const sd = stats.stdev(values)
  const scaled = o.standardize && sd > 0 ? values.map((v) => (v - mean) / sd) : values
  // qqPoints sorts ascending; a stable sort of the rows by value lines them up with it
  const order = ok.map((d, i) => ({ d, s: scaled[i]! })).sort((a, b) => a.s - b.s)
  const qq = stats.qqPoints(scaled)
  const points = qq.map((q, i) => ({ d: order[i]!.d, theoretical: q.theoretical, sample: q.sample, rank: i + 1 }))
  return { points, line: stats.qqLine(scaled), r: stats.pearson(qq.map((q) => q.theoretical), qq.map((q) => q.sample)), nulls, mean, sd }
}

/** "close to normal (r = 0.99)", "heavy tails (r = 0.93)", "skewed right ...", or '' when undefined. */
function shapeOf(m: Model, fmt: Formatters): string {
  const n = m.points.length
  if (n < 3 || !m.line || !Number.isFinite(m.r)) return ''
  const r = `r = ${fmt.num(m.r, 3)}`
  if (m.r >= 0.985) return `close to normal (${r})`
  const k = Math.max(1, Math.floor(n / 10))
  const resid = (p: QqPoint) => p.sample - (m.line!.intercept + m.line!.slope * p.theoretical)
  const lo = stats.mean(m.points.slice(0, k).map(resid))
  const hi = stats.mean(m.points.slice(n - k).map(resid))
  const shape = hi > 0 && lo < 0 ? 'heavy tails' : hi < 0 && lo > 0 ? 'light tails' : hi > 0 ? 'skewed right' : 'skewed left'
  return `${shape} (${r})`
}

export const qqPlot = defineChart<QqRow[], QqOptions>({
  id: 'qq-plot',
  name: 'Q-Q plot',
  category: 'distribution',
  intents: ['distribution'],
  summary: 'Sorted values against normal quantiles, with a reference line: straight means normal.',
  input,
  options,
  shape: 'rows',
  fields: ['value', 'label'],
  numberFields: ['value'],
  aspect: 0.75,
  isEmpty(data) {
    if (data.length === 0) return true
    return finiteValues(data).ok.length === 0 ? 'No values to plot' : false
  },
  draw(ctx) {
    const { theme, options: o, fmt } = ctx
    const m = model(ctx.data, o)
    const standardized = o.standardize && m.sd > 0
    const f = ctx.cartesian({
      x: { type: 'linear', domain: padExtent(finiteExtent(m.points.map((p) => p.theoretical)) ?? [-1, 1]), zero: false },
      y: { type: 'linear', domain: padExtent(finiteExtent(m.points.map((p) => p.sample)) ?? [0, 1]), zero: false },
      xFormat: (v: number) => fmt.num(v, 1),
      yFormat: standardized ? (v: number) => fmt.num(v, 1) : undefined,
      xTitle: o.xTitle,
      yTitle: o.yTitle ?? (standardized ? 'Sample z-score' : 'Sample quantile'),
      grid: 'both',
    })
    const marks: Mark[] = [...f.marks]
    if (o.showLine && m.line) {
      // clip y = a + b x to the plot's data rectangle
      const [xa, xb] = [Number(f.invertX(f.plot.x)), Number(f.invertX(f.plot.x + f.plot.width))]
      const [ya, yb] = [Number(f.invertY(f.plot.y + f.plot.height)), Number(f.invertY(f.plot.y))]
      const { slope: b, intercept: a } = m.line
      let lo = xa, hi = xb
      if (b !== 0) {
        const [s, t] = [(ya - a) / b, (yb - a) / b].sort((p, q) => p - q) as [number, number]
        lo = Math.max(lo, s)
        hi = Math.min(hi, t)
      } else if (a < ya || a > yb) hi = lo
      if (hi > lo) marks.push({ type: 'line', key: 'qq-line', x1: f.px(lo), y1: f.py(a + b * lo), x2: f.px(hi), y2: f.py(a + b * hi), stroke: theme.muted, strokeWidth: 1.5, dash: '5 4', inert: true })
    }
    const n = m.points.length
    const r = autoRadius(n, f.plot.width * f.plot.height, 1.25, 4.5, 0.12)
    const fill = theme.cat(0)
    for (const p of m.points) {
      const { row, value, index } = p.d
      const z = fmt.num(p.theoretical, 2)
      const shown = standardized ? `${fmt.value(value)} (z ${fmt.num(p.sample, 2)})` : fmt.value(value)
      marks.push({
        type: 'circle', role: 'point', key: `p:${index}`,
        cx: f.px(p.theoretical), cy: f.py(p.sample), r,
        fill, fillOpacity: 0.85, stroke: r >= 3 ? theme.surface : undefined, strokeWidth: r >= 3 ? 0.75 : undefined,
        datum: row,
        label: `${row.label ?? `Rank ${p.rank} of ${n}`}: ${shown} at normal quantile ${z}`,
        tooltip: { title: row.label ?? fmt.value(value), rows: [['Value', fmt.value(value)], ...(standardized ? [['z-score', fmt.num(p.sample, 2)] as [string, string]] : []), ['Normal quantile', z], ['Rank', `${p.rank} of ${n}`]], color: fill },
      })
    }
    return { marks, plot: f.plot, note: notes([[m.nulls, 'missing values skipped']]) }
  },
  describe({ data, options: o, fmt, name }) {
    const m = model(data, o)
    const shape = shapeOf(m, fmt)
    return `${name}, ${phrase.count(m.points.length, 'value')} against a normal distribution, ${spread(m.points.map((p) => p.d.value), fmt)}${shape ? `, ${shape}` : ''}`
  },
  table(data, o) {
    const m = model(data, o)
    const labelled = data.some((r) => r.label !== undefined)
    return {
      columns: [...(labelled ? ['Label'] : []), 'Value', 'Normal quantile'],
      rows: m.points.map((p) => [...(labelled ? [p.d.row.label ?? null] : []), p.d.value, p.theoretical]),
    }
  },
})

export default qqPlot
