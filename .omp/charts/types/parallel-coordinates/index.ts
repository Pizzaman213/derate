// Parallel coordinates: one vertical axis per numeric column, each with its own scale
// and ticks, and one polyline per row crossing them. Hovering a line highlights it;
// series colour the lines and toggle from the legend. Above 5k lines core paints them
// on a canvas and keeps the axes in SVG.

import { scaleLinear, type ScaleLinear } from 'd3-scale'
import { defineChart, phrase, SIZES, type Mark } from '../../core'
import { notes, uniq } from '../../core/helpers'
import { cellNum, numericColumns } from '../_shared/RelationshipB'
import { input, options, type PcOptions, type PcRow } from './schema'

const MAX_AXES = 12
const RESERVED = ['series', 'label']
/** Each line is its own highlight group, so hovering one dims the rest. */
const LINE_KEY = '#row:'
/**
 * Width of the transparent stroke that receives the pointer for each line: wide enough
 * that a 1.5 px line does not need pixel-hunting, narrow enough that in a crowded plot
 * the lines painted later do not bury the earlier ones entirely.
 */
const HIT_WIDTH = 4

const pickOf = (rows: PcRow[], o: { columns?: unknown }) =>
  numericColumns(rows, RESERVED, Array.isArray(o.columns) ? o.columns.filter((c): c is string => typeof c === 'string') : undefined, MAX_AXES)

const nameOf = (r: PcRow, i: number) => r.label ?? `Row ${i + 1}`

export const parallelCoordinates = defineChart<PcRow[], PcOptions>({
  id: 'parallel-coordinates',
  name: 'Parallel coordinates',
  category: 'relationship',
  intents: ['relationship', 'correlation'],
  summary: 'One vertical axis per numeric column and one line per row crossing them all.',
  input,
  options,
  shape: 'rows',
  fields: ['series', 'label'],
  aspect: 0.55,
  minHeight: 220,
  adapt(raw, o) {
    if (Array.isArray(raw) && raw.length) pickOf(raw as PcRow[], o)
    return raw
  },
  isEmpty: (rows, o) => (rows.length === 0 ? true : pickOf(rows, o).columns.length === 0 ? 'No numeric columns to plot' : false),
  draw(ctx) {
    const { theme, options: o, fmt, data: rows } = ctx
    const W = ctx.width, H = ctx.height, FS = SIZES.tick, LS = SIZES.label
    const { columns: dims, extra } = pickOf(rows, o)
    const k = dims.length
    const vals = dims.map((c) => rows.map((r) => cellNum(r, c)))
    const hasSeries = rows.some((r) => r.series !== undefined)
    const seriesList = hasSeries ? uniq(rows.map((r) => r.series ?? '(blank)')) : []
    const sIdx = new Map(seriesList.map((s, i) => [s, i]))

    // vertical layout: names on top, then the axes
    const top = LS + 12, bottom = Math.ceil(FS / 2) + 6
    const yRange: [number, number] = [H - bottom, top]
    const ys: Array<ScaleLinear<number, number>> = vals.map((v) => {
      const ok = v.filter((x): x is number => x !== null)
      let lo = ok.length ? Math.min(...ok) : 0, hi = ok.length ? Math.max(...ok) : 1
      if (o.zero) { lo = Math.min(0, lo); hi = Math.max(0, hi) }
      if (lo === hi) { const d = Math.abs(lo) * 0.1 || 1; lo -= d; hi += d }
      return scaleLinear().domain([lo, hi]).range(yRange).nice()
    })
    const tickN = Math.max(2, Math.min(8, Math.round((yRange[0] - yRange[1]) / 44)))
    const ticks = ys.map((y) => y.ticks(tickN))
    const tickW = ticks.map((t) => Math.max(0, ...t.map((v) => ctx.measure(fmt.value(v)))))

    // horizontal layout: tick labels sit left of each axis; the first one sets the margin
    const left = Math.ceil(tickW[0]! + 10)
    const lastName = ctx.measure(dims[k - 1]!, LS, 600)
    const right = Math.max(10, Math.min(lastName / 2, 60) + 4)
    const span = Math.max(1, W - left - right)
    const xs = dims.map((_, j) => (k === 1 ? left + span / 2 : left + (span * j) / (k - 1)))
    const step = k === 1 ? span : span / (k - 1)

    const marks: Mark[] = []
    // axes, ticks, names
    dims.forEach((d, j) => {
      const x = xs[j]!
      marks.push({ type: 'line', layer: 'axes', key: `ax:${d}`, x1: x, x2: x, y1: yRange[1], y2: yRange[0], stroke: theme.axis, strokeWidth: 1, inert: true })
      // thin ticks whose labels would collide with the previous one
      let lastY = Infinity
      for (const t of ticks[j]!) {
        const y = ys[j]!(t)
        if (lastY - y < FS * 1.3) continue
        lastY = y
        marks.push({ type: 'line', layer: 'axes', key: `tk:${d}:${t}`, x1: x - 4, x2: x, y1: y, y2: y, stroke: theme.axis, strokeWidth: 1, inert: true })
        marks.push({ type: 'text', layer: 'axes', key: `tl:${d}:${t}`, x: x - 6, y, text: fmt.value(t), anchor: 'end', baseline: 'central', size: FS, fill: theme.inkSecondary, halo: true, inert: true })
      }
      const room = Math.max(12, Math.min(step - 8, 2 * (x - 2), 2 * (W - x - 2)))
      const t = ctx.truncate(d, room, LS)
      marks.push({ type: 'text', layer: 'axes', key: `nm:${d}`, x, y: LS + 2, text: t, full: t !== d ? d : undefined, anchor: 'middle', baseline: 'alphabetic', size: LS, weight: 600, fill: theme.ink, className: t !== d ? 'oc-trunc' : undefined })
    })

    // lines
    const visibleRows = rows.map((r) => !hasSeries || !ctx.hidden.has(r.series ?? '(blank)'))
    const shown = visibleRows.filter(Boolean).length
    const width = shown > 1000 ? 1 : 1.5
    const alpha = shown > 2000 ? 0.2 : shown > 300 ? 0.4 : shown > 60 ? 0.6 : 0.85
    let gaps = 0, blank = 0, dupes = 0
    // identical rows would draw the same line on top of itself: draw it once, with a count
    const idOf = (r: PcRow, i: number) => JSON.stringify([r.label ?? null, r.series ?? null, ...vals.map((v) => v[i] ?? null)])
    const firstOf = new Map<string, { i: number; n: number }>()
    rows.forEach((r, i) => {
      const id = idOf(r, i)
      const hit = firstOf.get(id)
      if (hit) hit.n++
      else firstOf.set(id, { i, n: 1 })
    })
    rows.forEach((r, i) => {
      if (!visibleRows[i]) return
      const same = firstOf.get(idOf(r, i))!
      if (same.i !== i) { dupes++; return }
      let d = '', pen = false, nPts = 0
      let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity
      const pts: Array<[number, number]> = []
      let missing = false
      dims.forEach((_, j) => {
        const v = vals[j]![i]
        if (v == null) { pen = false; missing = true; return }
        const x = xs[j]!, y = ys[j]!(v)
        // an isolated value (its neighbours are missing) gets a short tick so it stays visible
        const nextOk = j + 1 < k && vals[j + 1]![i] != null
        if (!pen && !nextOk) d += `M${(x - 4).toFixed(2)},${y.toFixed(2)}h8`
        else d += `${pen ? 'L' : 'M'}${x.toFixed(2)},${y.toFixed(2)}`
        pen = true
        nPts++
        pts.push([x, y])
        x0 = Math.min(x0, x); x1 = Math.max(x1, x); y0 = Math.min(y0, y); y1 = Math.max(y1, y)
      })
      if (!nPts) { blank++; return }
      if (missing) gaps++
      const series = hasSeries ? r.series ?? '(blank)' : undefined
      const color = series !== undefined ? theme.cat(sIdx.get(series) ?? 0) : theme.cat(0)
      const name = nameOf(r, i)
      const mid = pts[Math.floor((pts.length - 1) / 2)]!
      const parts = dims.map((c, j) => `${c} ${vals[j]![i] == null ? 'missing' : fmt.value(vals[j]![i]!)}`)
      const lineSeries = `${LINE_KEY}${i}`
      // the visible line is a decoration; the data mark above it is the same path with a
      // wide transparent stroke, so a 1.5 px line can be hovered without pixel-hunting
      marks.push({ type: 'path', key: `v:${i}`, d, fill: 'none', stroke: color, strokeWidth: width, strokeOpacity: alpha, linejoin: 'round', linecap: 'round', series: lineSeries, inert: true })
      marks.push({
        type: 'path', role: 'line', key: `l:${i}`, d, fill: 'none', stroke: 'transparent', strokeWidth: HIT_WIDTH, linejoin: 'round', linecap: 'round',
        series: lineSeries, cx: mid[0], cy: mid[1], box: { x: x0 - 2, y: y0 - 2, width: x1 - x0 + 4, height: y1 - y0 + 4 },
        datum: r, label: `${name}${series !== undefined ? ` (${series})` : ''}: ${parts.join(', ')}`,
        tooltip: { title: name, rows: [...(series !== undefined ? [['Series', series] as [string, string]] : []), ...dims.map((c, j): [string, string] => [c, vals[j]![i] == null ? '–' : fmt.value(vals[j]![i]!)]), ...(same.n > 1 ? [['Identical rows', fmt.integer(same.n)] as [string, string]] : [])], color },
      })
    })
    return {
      marks,
      legend: seriesList.length > 1 ? { items: seriesList.map((s, i) => ({ key: s, label: s, color: theme.cat(i), shape: 'line' as const })), toggle: true } : undefined,
      note: notes([[gaps, `${gaps === 1 ? 'row has' : 'rows have'} missing values (the line breaks there)`], [blank, `${blank === 1 ? 'row' : 'rows'} with no values left out`], [dupes, `identical ${dupes === 1 ? 'row' : 'rows'} drawn once`], [extra.length, `more numeric ${extra.length === 1 ? 'column' : 'columns'} not shown (pick with the columns option)`]]),
      plot: { x: left, y: top, width: span, height: yRange[0] - yRange[1] },
    }
  },
  describe({ data: rows, options: o, name }) {
    const { columns: dims } = pickOf(rows, o)
    const series = uniq(rows.map((r) => r.series).filter((s): s is string => s !== undefined))
    return `${name}, ${phrase.count(rows.length, 'row')} across ${phrase.count(dims.length, 'axis', 'axes')} (${phrase.listPhrase(dims, 6)})${series.length > 1 ? `, ${series.length} series` : ''}`
  },
  table(rows, o) {
    const { columns: dims } = pickOf(rows, o)
    const hasSeries = rows.some((r) => r.series !== undefined)
    const hasLabel = rows.some((r) => r.label !== undefined)
    return {
      columns: [...(hasLabel ? ['Label'] : []), ...(hasSeries ? ['Series'] : []), ...dims],
      rows: rows.map((r) => [...(hasLabel ? [r.label ?? null] : []), ...(hasSeries ? [r.series ?? null] : []), ...dims.map((c) => cellNum(r, c))]),
    }
  },
})

export default parallelCoordinates
