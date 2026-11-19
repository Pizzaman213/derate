// Scatter-plot matrix: every pair of numeric columns as a small scatter, each column's
// distribution as a histogram on the diagonal. Scales are shared per column, so a point's
// x position means the same thing all the way down a column (and y along a row).

import { scaleLinear } from 'd3-scale'
import { bin } from 'd3-array'
import { defineChart, phrase, SIZES, type Mark } from '../../core'
import { notes, uniq } from '../../core/helpers'
import { cellNum, numericColumns } from '../_shared/RelationshipB'
import { input, options, type SplomOptions, type SplomRow } from './schema'

const MAX_COLUMNS = 6
const RESERVED = ['series', 'label']

const pickOf = (rows: SplomRow[], o: { columns?: unknown }) =>
  numericColumns(rows, RESERVED, Array.isArray(o.columns) ? o.columns.filter((c): c is string => typeof c === 'string') : undefined, MAX_COLUMNS)

const nameOf = (r: SplomRow, i: number) => r.label ?? (r.series !== undefined ? `${r.series} #${i + 1}` : `Row ${i + 1}`)

export const splom = defineChart<SplomRow[], SplomOptions>({
  id: 'splom',
  name: 'Scatter-plot matrix',
  category: 'relationship',
  intents: ['relationship', 'correlation'],
  summary: 'Small scatter plots of every pair of numeric columns, with histograms on the diagonal.',
  input,
  options,
  shape: 'rows',
  fields: ['series', 'label'],
  aspect: 0.9,
  minHeight: 240,
  maxHeight: 900,
  adapt(raw, o) {
    // an unknown or text column in `columns` is an input error, not an empty chart
    if (Array.isArray(raw) && raw.length) pickOf(raw as SplomRow[], o)
    return raw
  },
  isEmpty: (rows, o) => (rows.length === 0 ? true : pickOf(rows, o).columns.length === 0 ? 'No numeric columns to plot' : false),
  draw(ctx) {
    const { theme, options: o, fmt, data: rows } = ctx
    const W = ctx.width, H = ctx.height, FS = SIZES.tick
    const { columns: cols, extra } = pickOf(rows, o)
    const k = cols.length
    const vals = cols.map((c) => rows.map((r) => cellNum(r, c)))
    const hasSeries = rows.some((r) => r.series !== undefined)
    const seriesList = hasSeries ? uniq(rows.map((r) => r.series ?? '(blank)')) : []
    const sIdx = new Map(seriesList.map((s, i) => [s, i]))
    const seriesOf = (r: SplomRow) => (hasSeries ? r.series ?? '(blank)' : undefined)
    const visible = rows.map((r) => { const s = seriesOf(r); return s === undefined || !ctx.hidden.has(s) })

    // domains: every column's own extent, padded, never zero-width; nice ticks from it
    const scales0 = vals.map((v) => {
      const ok = v.filter((x): x is number => x !== null)
      let lo = Math.min(...ok), hi = Math.max(...ok)
      if (!ok.length) { lo = 0; hi = 1 }
      if (lo === hi) { const d = Math.abs(lo) * 0.1 || 1; lo -= d; hi += d }
      const pad = (hi - lo) * 0.04
      return scaleLinear().domain([lo - pad, hi + pad])
    })

    // layout: names outside the grid; tick labels only when cells can carry them
    const nameBand = SIZES.label + 10
    const tickLabelW = (j: number) => Math.max(...[3, 4, 5].flatMap((n) => scales0[j]!.ticks(n)).map((t) => ctx.measure(fmt.value(t))))
    const sizeFor = (ticks: boolean, gap: number) => {
      const left = nameBand + (ticks ? Math.ceil(Math.max(...cols.map((_, i) => tickLabelW(i)))) + 6 : 0)
      const bottom = nameBand + (ticks ? FS + 6 : 0)
      const s = Math.min((W - left - 8 - gap * (k - 1)) / k, (H - 6 - bottom - gap * (k - 1)) / k)
      return { left, bottom, s: Math.max(4, s) }
    }
    let gap = 8
    let lay = sizeFor(true, gap)
    let ticks = lay.s >= 72
    if (!ticks) { gap = 4; lay = sizeFor(false, gap) }
    const s = lay.s
    const blockW = k * s + (k - 1) * gap
    const x0 = lay.left + Math.max(0, (W - 8 - lay.left - blockW) / 2), y0 = 6
    const X = (j: number) => x0 + j * (s + gap), Y = (i: number) => y0 + i * (s + gap)
    const inset = Math.min(6, s * 0.06)
    const sx = (j: number, v: number) => X(j) + inset + scales0[j]!.copy().range([0, s - 2 * inset])(v)
    const sy = (i: number, v: number) => Y(i) + inset + scales0[i]!.copy().range([s - 2 * inset, 0])(v)
    const tickCount = s >= 160 ? 5 : s >= 110 ? 4 : 3

    const marks: Mark[] = []
    // frames, gridlines, tick labels, names
    for (let i = 0; i < k; i++) for (let j = 0; j < k; j++) {
      marks.push({ type: 'rect', layer: 'back', key: `f:${cols[i]}×${cols[j]}`, x: X(j), y: Y(i), width: s, height: s, fill: i === j ? theme.surfaceRaised : 'none', stroke: theme.grid, strokeWidth: 1, inert: true })
    }
    if (ticks) {
      cols.forEach((c, j) => {
        const tv = scales0[j]!.ticks(tickCount)
        // vertical gridlines down column j, horizontal along row j
        for (const t of tv) {
          const px = sx(j, t), py = sy(j, t)
          for (let i = 0; i < k; i++) {
            if (i !== j) marks.push({ type: 'line', layer: 'grid', key: `gx:${c}:${i}:${t}`, x1: px, x2: px, y1: Y(i), y2: Y(i) + s, stroke: theme.grid, strokeWidth: 0.5, opacity: 0.6, inert: true })
            if (i !== j) marks.push({ type: 'line', layer: 'grid', key: `gy:${c}:${i}:${t}`, y1: py, y2: py, x1: X(i), x2: X(i) + s, stroke: theme.grid, strokeWidth: 0.5, opacity: 0.6, inert: true })
          }
        }
        // x tick labels under the bottom row, thinned so neighbours never touch
        let lastRight = -Infinity
        for (const t of tv) {
          const text = fmt.value(t), w = ctx.measure(text), px = sx(j, t)
          if (px - w / 2 < X(j) - gap / 2 + 1 || px + w / 2 > X(j) + s + gap / 2 - 1 || px - w / 2 < lastRight + 6) continue
          lastRight = px + w / 2
          marks.push({ type: 'text', layer: 'axes', key: `xt:${c}:${t}`, x: px, y: Y(k - 1) + s + 4, text, anchor: 'middle', baseline: 'hanging', size: FS, fill: theme.inkSecondary })
        }
        // y tick labels left of the first column
        let lastTop = Infinity
        for (const t of tv) {
          const py = sy(j, t)
          if (py - FS * 0.6 < Y(j) - gap / 2 || py + FS * 0.6 > Y(j) + s + gap / 2 || py + FS * 0.6 > lastTop - 2) continue
          lastTop = py - FS * 0.6
          marks.push({ type: 'text', layer: 'axes', key: `yt:${c}:${t}`, x: X(0) - 5, y: py, text: fmt.value(t), anchor: 'end', baseline: 'central', size: FS, fill: theme.inkSecondary })
        }
      })
    }
    cols.forEach((c, j) => {
      const t = ctx.truncate(c, s, SIZES.label)
      const full = t !== c ? c : undefined
      const cls = full ? 'oc-trunc' : undefined
      marks.push({ type: 'text', layer: 'axes', key: `xn:${c}`, x: X(j) + s / 2, y: Y(k - 1) + s + (ticks ? FS + 6 : 0) + SIZES.label + 4, text: t, full, anchor: 'middle', baseline: 'alphabetic', size: SIZES.label, fill: theme.ink, weight: 600, className: cls })
      marks.push({ type: 'text', layer: 'axes', key: `yn:${c}`, x: x0 - lay.left + 4 + SIZES.label / 2, y: Y(j) + s / 2, text: t, full, anchor: 'middle', baseline: 'central', rotate: -90, size: SIZES.label, fill: theme.ink, weight: 600, className: cls })
    })

    // diagonal: histograms (stacked by series), of the visible rows
    cols.forEach((c, j) => {
      const dom = scales0[j]!.domain() as [number, number]
      const thresholds = scaleLinear().domain(dom).ticks(o.bins)
      const groups = hasSeries ? seriesList.filter((sn) => !ctx.hidden.has(sn)) : ['']
      const binsBy = groups.map((g) => bin().domain(dom).thresholds(thresholds)(rows.flatMap((r, ri) => {
        const v = vals[j]![ri]
        return v != null && visible[ri] && (!hasSeries || seriesOf(r) === g) ? [v] : []
      })))
      const nb = binsBy[0]?.length ?? 0
      let max = 1
      for (let b = 0; b < nb; b++) max = Math.max(max, binsBy.reduce((n, bs) => n + (bs[b]?.length ?? 0), 0))
      const hMax = (s - 2 * inset) * 0.92
      for (let b = 0; b < nb; b++) {
        let acc = 0
        groups.forEach((g, gi) => {
          const bb = binsBy[gi]![b]!
          const n = bb.length
          if (!n) return
          const xa = sx(j, bb.x0!), xb = sx(j, bb.x1!)
          const h = (n / max) * hMax
          const yb = Y(j) + s - inset - acc
          acc += h
          const color = hasSeries ? theme.cat(sIdx.get(g) ?? 0) : theme.cat(0)
          const range = `${fmt.value(bb.x0!)} to ${fmt.value(bb.x1!)}`
          marks.push({
            type: 'rect', role: 'bar', key: `h:${c}:${g}:${b}`, x: xa + 0.5, y: yb - h, width: Math.max(0.5, xb - xa - 1), height: h,
            fill: color, fillOpacity: 0.85, series: hasSeries ? g : undefined,
            datum: { column: c, x0: bb.x0, x1: bb.x1, count: n, series: hasSeries ? g : undefined },
            label: `${c} ${range}: ${n} ${n === 1 ? 'row' : 'rows'}${hasSeries ? ` (${g})` : ''}`,
            tooltip: { title: c, rows: [['Range', range], ['Rows', fmt.integer(n)], ...(hasSeries ? [['Series', g] as [string, string]] : [])], color },
          })
        })
      }
    })

    // off-diagonal: one point per row with both values
    const n = rows.length
    const r = n > 2000 ? 1.2 : s < 60 ? 1.6 : s < 120 ? 2.2 : 3
    const opacity = n > 2000 ? 0.45 : n > 300 ? 0.6 : 0.8
    const missing = rows.filter((_, ri) => vals.some((v) => v[ri] == null)).length
    for (let i = 0; i < k; i++) for (let j = 0; j < k; j++) {
      if (i === j) continue
      rows.forEach((row, ri) => {
        const xv = vals[j]![ri], yv = vals[i]![ri]
        if (xv == null || yv == null) return
        const series = seriesOf(row)
        const color = series !== undefined ? theme.cat(sIdx.get(series) ?? 0) : theme.cat(0)
        const name = nameOf(row, ri)
        marks.push({
          type: 'circle', role: 'point', key: `p:${cols[j]}×${cols[i]}:${ri}`, cx: sx(j, xv), cy: sy(i, yv), r, fill: color, fillOpacity: opacity, series,
          datum: row, label: `${name}: ${cols[j]} ${fmt.value(xv)}, ${cols[i]} ${fmt.value(yv)}`,
          tooltip: { title: name, rows: [[cols[j]!, fmt.value(xv)], [cols[i]!, fmt.value(yv)], ...(series !== undefined ? [['Series', series] as [string, string]] : [])], color },
        })
      })
    }
    return {
      marks,
      legend: seriesList.length > 1 ? { items: seriesList.map((sn, i) => ({ key: sn, label: sn, color: theme.cat(i), shape: 'circle' as const })) } : undefined,
      note: notes([[missing, `${missing === 1 ? 'row has' : 'rows have'} a missing value, left out of the plots that need it`], [extra.length, `more numeric ${extra.length === 1 ? 'column' : 'columns'} not shown (pick with the columns option)`]]),
      plot: { x: x0, y: y0, width: blockW, height: blockW },
    }
  },
  describe({ data: rows, options: o, name }) {
    const { columns: cols } = pickOf(rows, o)
    const series = uniq(rows.map((r) => r.series).filter((s): s is string => s !== undefined))
    return `${name}, ${phrase.count(rows.length, 'row')} across ${phrase.count(cols.length, 'column')} (${phrase.listPhrase(cols, 6)})${series.length > 1 ? `, ${series.length} series` : ''}`
  },
  table(rows, o) {
    const { columns: cols } = pickOf(rows, o)
    const hasSeries = rows.some((r) => r.series !== undefined)
    const hasLabel = rows.some((r) => r.label !== undefined)
    return {
      columns: [...(hasLabel ? ['Label'] : []), ...(hasSeries ? ['Series'] : []), ...cols],
      rows: rows.map((r) => [...(hasLabel ? [r.label ?? null] : []), ...(hasSeries ? [r.series ?? null] : []), ...cols.map((c) => cellNum(r, c))]),
    }
  },
})

export default splom
