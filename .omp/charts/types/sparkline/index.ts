// Sparkline: a word-sized line with no axes; the last point is dotted, and
// optionally the lowest and highest.

import { defineChart, SIZES, type Formatters, type Mark } from '../../core'
import { notes } from '../../core/helpers'
import { linePath, pointDateFormatter } from '../_shared/TrendB'
import { input, options, type SparklineOptions, type SparklineRow } from './schema'

export interface SparkPoint {
  /** Position on x: epoch ms for dates, the number for indices, row order otherwise. */
  t: number
  /** Display label of x. */
  at: string
  y: number | null
  row: SparklineRow
}

/** Rows -> points ordered along x, with an x label for each. */
export function sparkPoints(data: SparklineRow[], fmt: Formatters): SparkPoint[] {
  const dates = data.every((r) => r.x instanceof Date)
  const nums = !dates && data.every((r) => typeof r.x === 'number')
  if (dates) {
    const f = pointDateFormatter(data.map((r) => r.x as Date), fmt)
    return data.map((row) => ({ t: +(row.x as Date), at: f(row.x as Date), y: row.y, row })).sort((a, b) => a.t - b.t)
  }
  if (nums) return data.map((row) => ({ t: row.x as number, at: `#${fmt.num(row.x as number)}`, y: row.y, row })).sort((a, b) => a.t - b.t)
  return data.map((row, i) => ({ t: i + 1, at: `#${i + 1}`, y: row.y, row }))
}

interface Stats { first: SparkPoint; last: SparkPoint; min: SparkPoint; max: SparkPoint }
function stats(pts: SparkPoint[]): Stats | null {
  const ok = pts.filter((p) => p.y !== null)
  if (!ok.length) return null
  let min = ok[0]!, max = ok[0]!
  for (const p of ok) {
    if (p.y! < min.y!) min = p
    if (p.y! > max.y!) max = p
  }
  return { first: ok[0]!, last: ok[ok.length - 1]!, min, max }
}

export const sparkline = defineChart<SparklineRow[], SparklineOptions>({
  id: 'sparkline',
  name: 'Sparkline',
  category: 'trend',
  intents: ['trend', 'change-over-time', 'kpi'],
  summary: 'A word-sized line showing the shape of a series, no axes.',
  input,
  options,
  shape: 'rows',
  fields: ['x', 'y'],
  dateFields: ['x'],
  numberFields: ['y'],
  aspect: 0.1,
  minHeight: 32,
  maxHeight: 48,
  isEmpty: (data) => data.length === 0 || data.every((r) => r.y === null),
  draw(ctx) {
    const { theme, options: o, fmt, width: W, height: H } = ctx
    const pts = sparkPoints(ctx.data, fmt)
    const s = stats(pts)!
    const dot = Math.max(1.5, Math.min(2.5, H / 14))
    const lastText = fmt.value(s.last.y)
    const valueW = o.showLastValue ? ctx.measure(lastText) + 6 : 0
    const left = dot + 1, right = W - dot - 1 - valueW
    const top = dot + 1, bottom = H - dot - 1
    const t0 = pts[0]!.t, t1 = pts[pts.length - 1]!.t
    const px = (t: number) => (t1 === t0 ? (left + right) / 2 : left + ((t - t0) / (t1 - t0)) * (right - left))
    const lo = s.min.y!, hi = s.max.y!
    const py = (y: number) => (hi === lo ? (top + bottom) / 2 : bottom - ((y - lo) / (hi - lo)) * (bottom - top))
    const color = theme.cat(0)
    const marks: Mark[] = []
    const line = pts.map((p) => (p.y === null ? null : ([px(p.t), py(p.y)] as const)))
    if (o.area) {
      // one closed area per unbroken run
      let run: Array<readonly [number, number]> = []
      const runs: Array<typeof run> = []
      for (const p of line) {
        if (p) run.push(p)
        else if (run.length) { runs.push(run); run = [] }
      }
      if (run.length) runs.push(run)
      const base = hi === lo ? bottom : py(Math.max(lo, Math.min(hi, 0)))
      const d = runs.map((r) => `${linePath(r)}L${r[r.length - 1]![0]},${base}L${r[0]![0]},${base}Z`).join('')
      marks.push({ type: 'path', key: 'area', role: 'area', d, fill: color, fillOpacity: 0.16, inert: true })
    }
    marks.push({ type: 'path', key: 'line', role: 'line', d: linePath(line), stroke: color, strokeWidth: 1.5, fill: 'none', linejoin: 'round', linecap: 'round', inert: true })
    // a lone point between two gaps would vanish: give it a dot
    const lone = new Set(line.flatMap((p, i) => (p && !line[i - 1] && !line[i + 1] ? [i] : [])))
    const step = pts.length > 1 ? (right - left) / (pts.length - 1) : right - left
    const hitR = Math.max(1, Math.min(4, step / 2))
    pts.forEach((p, i) => {
      if (p.y === null) return
      const isLast = p === s.last, isMin = o.showMinMax && p === s.min && hi !== lo, isMax = o.showMinMax && p === s.max && hi !== lo
      const shown = isLast || isMin || isMax || lone.has(i)
      const fill = isLast ? color : isMax ? theme.positive : isMin ? theme.negative : color
      const what = [isLast && 'last', isMax && 'highest', isMin && 'lowest'].filter(Boolean).join(', ')
      marks.push({
        type: 'circle', role: 'point', key: `p:${p.t}:${i}`, cx: px(p.t), cy: py(p.y), r: shown ? dot : hitR,
        fill, fillOpacity: shown ? 1 : 0, stroke: shown ? theme.surface : undefined, strokeWidth: shown ? 1 : undefined,
        datum: p.row, label: `${p.at}: ${fmt.value(p.y)}${what ? ` (${what})` : ''}`,
        tooltip: { title: p.at, rows: [['Value', fmt.value(p.y)]], color: fill },
      })
    })
    if (o.showLastValue) {
      const ly = Math.min(H - SIZES.tick / 2 - 2, Math.max(SIZES.tick / 2 + 2, py(s.last.y!)))
      marks.push({ type: 'text', layer: 'labels', key: 'last-value', x: W - valueW + 4, y: ly, text: lastText, anchor: 'start', baseline: 'central', fill: theme.ink, weight: 600 })
    }
    const nulls = pts.filter((p) => p.y === null).length
    const dupes = pts.length - new Set(pts.map((p) => p.t)).size
    return { marks, note: notes([[nulls, 'missing values left as gaps'], [dupes, 'repeated x values kept as separate points']]) }
  },
  describe({ data, fmt, name }) {
    const pts = sparkPoints(data, fmt)
    const s = stats(pts)
    if (!s) return `${name}, no values`
    const n = pts.filter((p) => p.y !== null).length
    const span = s.first === s.last ? `at ${s.first.at}` : `${s.first.at} to ${s.last.at}`
    return `${name}, ${n} ${n === 1 ? 'point' : 'points'}, ${span}, first ${fmt.value(s.first.y)}, last ${fmt.value(s.last.y)}, min ${fmt.value(s.min.y)} at ${s.min.at}, max ${fmt.value(s.max.y)} at ${s.max.at}`
  },
  table(data, _o, fmt) {
    return { columns: ['X', 'Value'], rows: sparkPoints(data, fmt).map((p) => [p.at, p.y]) }
  },
})

export default sparkline
