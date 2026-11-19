// Shared by the KPI charts (gauge, progress-ring, linear-progress, kpi-tile, meter,
// small-multiples): object-or-array input, grid fitting, arcs, unique keys, and
// the label | track | value row layout.

import { arc as d3arc } from 'd3-shape'
import { z } from 'zod'
import { isRecord, mapFields, toRows } from '../../adapters'
import { SIZES, type BaseOptions, type DrawContext, type Mark, type Palette, type Paint } from '../../core'


/**
 * KPI input: one object, an array of objects, CSV text or column arrays (each row one
 * item), with `options.fields` mapping canonical names to columns.
 */
export function adaptObjects(raw: unknown, options: BaseOptions): unknown {
  if (raw === null || raw === undefined) return []
  let list: unknown = raw
  if (typeof raw === 'string') list = toRows(raw) ?? raw
  else if (isRecord(raw)) {
    const values = Object.values(raw)
    list = values.length > 0 && values.every(Array.isArray) ? toRows(raw) ?? [raw] : [raw]
  } else if (Array.isArray(raw) && raw.length > 0 && raw.every(Array.isArray)) list = toRows(raw) ?? raw
  const fields = options.fields
  if (fields && Object.keys(fields).length && Array.isArray(list) && list.every(isRecord)) return mapFields(list, fields)
  return list
}

/** A list of KPI objects (adaptObjects wraps a single object). */
export const objectList = <T extends z.ZodTypeAny>(item: T, description: string) => z.array(item).describe(description)

/** Optional text that tolerates numbers and blanks. */
export const optText = (description: string) =>
  z.preprocess((v) => (typeof v === 'number' || typeof v === 'boolean' ? String(v) : v === null || v === '' ? undefined : v), z.string({ invalid_type_error: 'text' }).optional()).describe(description)

/** Optional number with a default; blanks and nulls take the default, numeric strings coerce. */
export const numDefault = (dflt: number, description: string) =>
  z.preprocess((v) => {
    if (v === null || v === undefined || v === '') return undefined
    if (typeof v === 'string' && v.trim() !== '' && Number.isFinite(Number(v.trim()))) return Number(v.trim())
    return v
  }, z.number({ invalid_type_error: 'number' }).finite().default(dflt)).describe(description)

/** Keys that stay unique when labels repeat: "A", "A#2", "A#3". */
export function uniqueKeys(labels: string[]): string[] {
  const seen = new Map<string, number>()
  return labels.map((l) => {
    const n = (seen.get(l) ?? 0) + 1
    seen.set(l, n)
    return n === 1 ? l : `${l}#${n}`
  })
}

export interface Grid {
  cols: number
  rows: number
  cellW: number
  cellH: number
  /** Items drawn. */
  shown: number
  /** Items that did not fit; `moreMarks` says so in the next cell (layout-independent notes stay notes). */
  more: number
  gap: number
}

/**
 * The column count that gives the biggest cells for n items in W x H (cells scored by
 * `score`, default min(cellW * ratio, cellH); ties go to more columns); items beyond what
 * fits at the minimum cell size are cut.
 */
export function fitGrid(n: number, W: number, H: number, o: { minW: number; minH: number; gap: number; ratio: number; maxCols?: number; score?: (cellW: number, cellH: number) => number }): Grid {
  const g = o.gap
  const maxCols = Math.max(1, Math.min(n, o.maxCols ?? n, Math.floor((W + g) / (o.minW + g)) || 1))
  let best: Grid | null = null
  let bestScore = -Infinity
  for (let cols = 1; cols <= maxCols; cols++) {
    const cellW = (W - g * (cols - 1)) / cols
    const fitRows = Math.max(1, Math.floor((H + g) / (o.minH + g)))
    const rows = Math.min(Math.ceil(n / cols), fitRows)
    const cellH = (H - g * (rows - 1)) / rows
    const capacity = rows * cols
    const shown = n <= capacity ? n : Math.max(1, capacity - 1)
    const score = shown * 1e6 + (o.score ? o.score(cellW, cellH) : Math.min(cellW * o.ratio, cellH))
    if (score >= bestScore) {
      bestScore = score
      best = { cols, rows, cellW, cellH, shown, more: n - shown, gap: g }
    }
  }
  return best ?? { cols: 1, rows: 1, cellW: W, cellH: H, shown: Math.min(n, 1), more: Math.max(0, n - 1), gap: g }
}

/** "+12 more" in the first unused cell (or the chart's bottom-right corner when there is none). */
export function moreMarks(ctx: Pick<DrawContext, 'theme' | 'width' | 'height' | 'truncate'>, grid: Grid, noun: string, x0 = 0, y0 = 0): Mark[] {
  if (grid.more <= 0) return []
  const head = `+${grid.more} more ${noun}`
  const free = grid.shown < grid.rows * grid.cols
  const c = cellAt(grid, grid.shown, x0, y0)
  const w = free ? grid.cellW - 8 : ctx.width / 2
  const x = free ? c.x + grid.cellW / 2 : ctx.width - 2
  const y = free ? c.y + grid.cellH / 2 : ctx.height - 4
  const anchor = free ? 'middle' : 'end'
  const t1 = ctx.truncate(head, w, SIZES.label)
  const out: Mark[] = [{ type: 'text', key: 'more', layer: 'labels', x, y: free ? y - 2 : y, text: t1, full: t1 !== head ? head : undefined, anchor, baseline: free ? 'alphabetic' : 'auto', size: SIZES.label, weight: 600, fill: ctx.theme.inkSecondary }]
  if (free) {
    const sub = 'all are in the data table'
    const t2 = ctx.truncate(sub, w, SIZES.tick)
    if (t2 === sub) out.push({ type: 'text', key: 'more-sub', layer: 'labels', x, y: y + 4, text: t2, anchor, baseline: 'hanging', size: SIZES.tick, fill: ctx.theme.muted, inert: true })
  }
  return out
}

/** Top-left of cell i (row-major). */
export function cellAt(grid: Grid, i: number, x0 = 0, y0 = 0): { x: number; y: number } {
  const c = i % grid.cols, r = Math.floor(i / grid.cols)
  return { x: x0 + c * (grid.cellW + grid.gap), y: y0 + r * (grid.cellH + grid.gap) }
}

const arcGen = d3arc<{ innerRadius: number; outerRadius: number; startAngle: number; endAngle: number; cornerRadius?: number }>()
  .cornerRadius((d) => d.cornerRadius ?? 0)

/** Annular sector; angles in radians clockwise from 12 o'clock, centred on (0, 0). */
export function arcPath(inner: number, outer: number, a0: number, a1: number, cornerRadius = 0): string {
  return arcGen({ innerRadius: Math.max(0, inner), outerRadius: Math.max(0, outer), startAngle: a0, endAngle: a1, cornerRadius }) ?? ''
}

/** Point at radius r and angle a (clockwise from 12 o'clock) around (0, 0). */
export function polar(r: number, a: number): [number, number] {
  return [r * Math.sin(a), -r * Math.cos(a)]
}

/** The largest font size <= max (and >= min) at which `text` fits in `width`. */
export function fitSize(ctx: Pick<DrawContext, 'measure'>, text: string, width: number, max: number, min: number, weight: number | string = 600): number {
  const w = ctx.measure(text, max, weight)
  if (w <= width) return max
  return Math.max(min, Math.floor(max * (width / w)))
}

export type Status = 'good' | 'warning' | 'bad'
export const STATUS_PATTERN: Record<Status, number> = { good: 2, warning: 1, bad: 0 }
export const statusPaint = (theme: Palette, s: Status | null): Paint => (s === 'good' ? theme.positive : s === 'warning' ? theme.warning : s === 'bad' ? theme.negative : theme.cat(0))
export const statusWord: Record<Status, string> = { good: 'good', warning: 'warning', bad: 'bad' }

/** Share of [min, max] covered by v, clamped to [0, 1]. */
export function share(v: number, min: number, max: number): number {
  if (!(max > min)) return 0
  return Math.min(1, Math.max(0, (v - min) / (max - min)))
}

export interface RowLayout {
  /** Label above the track (narrow) or to its left (wide). */
  stacked: boolean
  labelX: number
  labelW: number
  trackX: number
  trackW: number
  valueX: number
  valueW: number
  rowH: number
  trackH: number
  top: number
  /** Rows drawn; when `more` > 0 the row after them says "+N more". */
  shown: number
  more: number
}

/**
 * Rows of label | track | value, vertically centred; at narrow widths the label sits
 * above the track and the value at its right end.
 */
export function rowLayout(ctx: Pick<DrawContext, 'measure' | 'width' | 'height'>, labels: string[], values: string[], o: { labelSize: number; valueSize: number; maxRowH?: number }): RowLayout {
  const W = ctx.width, H = ctx.height
  const n = labels.length
  const stacked = W < 400
  const valueW = Math.ceil(Math.max(ctx.measure('100%', o.valueSize, 600), ...values.map((v) => ctx.measure(v, o.valueSize, 600))))
  const minRowH = stacked ? o.labelSize + 18 : Math.max(o.labelSize, o.valueSize) + 12
  const maxRowH = o.maxRowH ?? (stacked ? 56 : 44)
  const fit = Math.max(1, Math.floor(H / minRowH))
  const shown = n <= fit ? n : Math.max(1, fit - 1)
  const rows = shown + (n > shown ? 1 : 0)
  const rowH = Math.max(minRowH, Math.min(maxRowH, H / Math.max(1, rows)))
  const trackH = Math.max(6, Math.min(16, Math.round((stacked ? rowH - o.labelSize - 8 : rowH) * 0.42)))
  const top = Math.max(0, (H - rowH * rows) / 2)
  const more = n - shown
  if (stacked) {
    return { stacked, labelX: 0, labelW: Math.max(0, W - valueW - 8), trackX: 0, trackW: W, valueX: W, valueW, rowH, trackH, top, shown, more }
  }
  const longest = Math.max(0, ...labels.map((l) => ctx.measure(l, o.labelSize)))
  const labelW = Math.ceil(Math.min(longest, W * 0.3))
  const gap = 12
  const trackX = labelW > 0 ? labelW + gap : 0
  return { stacked, labelX: 0, labelW, trackX, trackW: Math.max(20, W - trackX - gap - valueW), valueX: W, valueW, rowH, trackH, top, shown, more }
}

/** The "+N more" line in the row after the last one drawn. */
export function moreRow(ctx: Pick<DrawContext, 'theme' | 'width' | 'truncate'>, L: RowLayout, noun: string): Mark[] {
  if (L.more <= 0) return []
  const text = ctx.truncate(`+${L.more} more ${noun} (all are in the data table)`, ctx.width - (L.stacked ? 0 : L.trackX), SIZES.tick)
  return [{ type: 'text', key: 'more', layer: 'labels', x: L.stacked ? 0 : L.trackX, y: L.top + L.shown * L.rowH + L.rowH / 2, text, anchor: 'start', baseline: 'central', size: SIZES.tick, fill: ctx.theme.muted, inert: true }]
}

const NO_SPACE = /^(%|‰|°[CFK]?|′|″)$/
/** "72%", "21.5°C", "340 ms": symbols attach, words get a space. */
export const withUnit = (text: string, unit: string | undefined) => (unit ? (NO_SPACE.test(unit) ? `${text}${unit}` : `${text} ${unit}`) : text)
