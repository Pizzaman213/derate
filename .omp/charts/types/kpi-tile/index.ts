// KPI tile: a labelled big number, its change against a previous value (absolute and
// percent, coloured by which direction is good, with an arrow drawn as a shape so the
// direction survives without colour), and an optional sparkline of its history.
// A list becomes a grid of tiles that wraps to the width.

import { defineChart, makeFormatters, phrase, SIZES, type Formatters, type Mark } from '../../core'
import { notes } from '../../core/helpers'
import { parseDate } from '../../time/parse'
import { adaptObjects, fitGrid, fitSize, moreMarks, uniqueKeys, withUnit } from '../_shared/KpiCharts'
import { input, options, type TileItem, type TileOptions } from './schema'

export interface Delta {
  /** value - previous */
  abs: number
  /** abs / |previous|; null when previous is 0 */
  pct: number | null
  dir: 'up' | 'down' | 'flat'
  tone: 'good' | 'bad' | 'neutral'
}

/** Change from previous to value, and whether it is good given the good direction. */
export function kpiDelta(value: number | null, previous: number | null, goodDirection: 'up' | 'down' = 'up'): Delta | null {
  if (value === null || previous === null) return null
  const abs = value - previous
  const pct = previous === 0 ? null : abs / Math.abs(previous)
  const dir = abs > 0 ? 'up' : abs < 0 ? 'down' : 'flat'
  const tone = dir === 'flat' ? 'neutral' : dir === goodDirection ? 'good' : 'bad'
  return { abs, pct, dir, tone }
}

/** The sparkline's y values in x order (points sort by number or date; plain numbers keep their order). */
export function seriesValues(series: TileItem['series']): Array<number | null> {
  if (!series?.length) return []
  const first = series[0]
  if (typeof first === 'number' || first === null) return series as Array<number | null>
  const pts = series as Array<{ x: number | string | Date; y: number | null }>
  const keyed = pts.map((p) => ({ k: typeof p.x === 'number' ? p.x : parseDate(p.x)?.getTime() ?? null, y: p.y }))
  if (keyed.every((p) => p.k !== null)) keyed.sort((a, b) => a.k! - b.k!)
  return keyed.map((p) => p.y)
}

const MINUS = '−'
/** "+1.2k (+8.3%)", "−40 ms (−12%)", "no change". */
export function deltaText(d: Delta, f: Formatters, unit: string | undefined, signed = true, value: (v: number) => string = f.value): string {
  if (d.dir === 'flat') return 'no change'
  const sign = !signed ? '' : d.abs > 0 ? '+' : MINUS
  const abs = `${sign}${withUnit(value(Math.abs(d.abs)), unit)}`
  if (d.pct === null) return abs
  return `${abs} (${sign}${f.pct(Math.abs(d.pct), Math.abs(d.pct) < 0.1 ? 1 : 0)})`
}

type FmtOpts = TileOptions & { locale?: string; currency?: string; timeZone?: string; valueFormat?: string }

/**
 * A tile's number formatter. The headline is the point of a tile, so 'auto' and
 * 'currency' keep full digits below a million (18,420, not 18k) and go compact above.
 */
export function tileFormatter(d: Pick<TileItem, 'format'>, o: FmtOpts): { value: (v: number) => string; change: (v: number) => string } {
  const vf = d.format ?? o.valueFormat ?? 'auto'
  const f = makeFormatters({ locale: o.locale, currency: o.currency, timeZone: o.timeZone, valueFormat: vf })
  if (vf === 'auto') {
    const value = (v: number) => (Math.abs(v) >= 1e6 ? f.si(v) : f.num(v, Math.abs(v) < 1 ? 3 : Math.abs(v) >= 1000 ? 0 : 2))
    return { value, change: value }
  }
  if (vf === 'currency') {
    const value = (v: number) => f.currency(v, Math.abs(v) >= 1e6)
    return { value, change: value }
  }
  // a change between two percentages is in percentage points, not percent
  if (vf === 'percent') return { value: f.value, change: (v) => `${f.num(v * 100, 1)} pp` }
  return { value: f.value, change: f.value }
}

/** Arrow glyph as a path: a triangle pointing up or down, or a bar for no change. */
function arrow(dir: Delta['dir'], x: number, cy: number, s: number): string {
  const h = s * 0.85
  if (dir === 'up') return `M${x},${cy + h / 2}L${x + s / 2},${cy - h / 2}L${x + s},${cy + h / 2}Z`
  if (dir === 'down') return `M${x},${cy - h / 2}L${x + s / 2},${cy + h / 2}L${x + s},${cy - h / 2}Z`
  return `M${x},${cy - 1.25}h${s}v2.5h${-s}Z`
}

const PAD = 12
const GAP = 12
const MAX_W = 340

export const kpiTile = defineChart<TileItem[], TileOptions>({
  id: 'kpi-tile',
  name: 'KPI tile',
  category: 'kpi',
  intents: ['kpi', 'trend'],
  summary: 'A big number with its change against a previous value and an optional sparkline; a list becomes a grid of tiles.',
  input,
  options,
  shape: 'object',
  fields: ['label', 'value', 'previous', 'unit'],
  adapt: adaptObjects,
  aspect: 0.32,
  minHeight: 260,
  maxHeight: 440,
  isEmpty: (data) => data.length === 0,
  draw(ctx) {
    const { theme, fmt, options: o, width: W, height: H } = ctx
    const items = ctx.data
    const n = items.length
    const keys = uniqueKeys(items.map((d) => d.label))
    const anySpark = items.some((d) => seriesValues(d.series).filter((v) => v !== null).length >= 2)
    const minH = SIZES.label + SIZES.value + SIZES.label + PAD * 2 + 12 + (anySpark ? 26 : 0)
    const naturalH = PAD * 2 + SIZES.label + 8 + SIZES.value + 8 + SIZES.label + 6 + (anySpark ? 52 : 0)
    const grid = fitGrid(n, W, H, { minW: 150, minH, gap: GAP, ratio: 0.5, score: (cw, ch) => Math.min(MAX_W, cw) * 0.5 + Math.min(naturalH, ch) })
    const tileW = Math.min(MAX_W, grid.cellW)
    const tileH = Math.min(naturalH, grid.cellH)
    const marks: Mark[] = []
    const toneP = { good: theme.positive, bad: theme.negative, neutral: theme.muted }
    for (let i = 0; i < grid.shown; i++) {
      const d = items[i]!, key = keys[i]!
      const c = i % grid.cols, r = Math.floor(i / grid.cols)
      const x = c * (tileW + GAP), y = r * (tileH + GAP)
      const fv = tileFormatter(d, o)
      const valueText = d.value === null ? '—' : withUnit(fv.value(d.value), d.unit)
      const delta = kpiDelta(d.value, d.previous, d.goodDirection)
      const dText = delta ? deltaText(delta, fmt, d.unit, true, fv.change) : null
      const tip: Array<[string, string]> = [['Value', valueText]]
      if (d.previous !== null) tip.push(['Previous', withUnit(fv.value(d.previous), d.unit)])
      if (delta && dText) tip.push(['Change', `${dText}${delta.tone !== 'neutral' ? ` (${delta.tone})` : ''}`])
      const spoken = delta && dText
        ? `${d.label}: ${valueText}, ${delta.dir === 'flat' ? 'no change' : `${delta.dir} ${deltaText(delta, fmt, d.unit, false, fv.change)}`} ${o.compareLabel}${delta.tone !== 'neutral' ? `, ${delta.tone}` : ''}`
        : `${d.label}: ${valueText}`
      marks.push({
        type: 'rect', role: 'tile', key: `${key}:tile`, x: x + 0.5, y: y + 0.5, width: tileW - 1, height: tileH - 1, rx: 4, fill: theme.surfaceRaised, stroke: theme.grid, strokeWidth: 1,
        datum: d, label: spoken, tooltip: { title: d.label, rows: tip, color: delta ? toneP[delta.tone] : undefined },
      })
      const inner = tileW - PAD * 2
      // label
      const lt = ctx.truncate(d.label, inner, SIZES.label)
      let cy = y + PAD
      marks.push({ type: 'text', key: `${key}:label`, layer: 'labels', x: x + PAD, y: cy, text: lt, full: lt !== d.label ? d.label : undefined, anchor: 'start', baseline: 'hanging', size: SIZES.label, fill: theme.inkSecondary, inert: lt === d.label })
      cy += SIZES.label + 8
      // big value
      const vs = fitSize(ctx, valueText, inner, SIZES.value, SIZES.title)
      const vt = ctx.truncate(valueText, inner * 0.92, vs) // truncate measures regular weight; this is semibold
      marks.push({ type: 'text', key: `${key}:value`, layer: 'labels', x: x + PAD, y: cy, text: vt, full: vt !== valueText ? valueText : undefined, anchor: 'start', baseline: 'hanging', size: vs, weight: 600, fill: d.value === null ? theme.muted : theme.ink, inert: vt === valueText })
      cy += vs + 8
      // delta row
      if (delta && dText) {
        const s = SIZES.tick
        const mid = cy + SIZES.label / 2
        const paint = toneP[delta.tone]
        marks.push({ type: 'path', key: `${key}:arrow`, layer: 'labels', d: arrow(delta.dir, x + PAD, mid, s), fill: paint, inert: true })
        const tx = x + PAD + s + 5
        const room = x + tileW - PAD - tx
        const dt = ctx.truncate(dText, room * 0.92, SIZES.label)
        marks.push({ type: 'text', key: `${key}:delta`, layer: 'labels', x: tx, y: mid, text: dt, full: dt !== dText ? dText : undefined, anchor: 'start', baseline: 'central', size: SIZES.label, weight: 600, fill: paint, inert: dt === dText })
        // the comparison words sit at the right edge, when they fit beside the change
        const used = ctx.measure(dt, SIZES.label, 600) + 10
        const cmp = o.compareLabel
        if (cmp && ctx.measure(cmp, SIZES.tick) <= room - used) {
          marks.push({ type: 'text', key: `${key}:cmp`, layer: 'labels', x: x + tileW - PAD, y: mid, text: cmp, anchor: 'end', baseline: 'central', size: SIZES.tick, fill: theme.muted, inert: true })
        }
        cy += SIZES.label + 6
      }
      // sparkline
      const ys = seriesValues(d.series)
      const ok = ys.filter((v): v is number => v !== null)
      const sparkH = y + tileH - PAD - (cy + 4)
      if (ok.length >= 2 && sparkH >= 12) {
        const lo = Math.min(...ok), hi = Math.max(...ok)
        const top = cy + 4, bottom = top + Math.min(sparkH, 48)
        const sx = (k: number) => x + PAD + (ys.length === 1 ? inner / 2 : (k / (ys.length - 1)) * inner)
        const sy = (v: number) => (hi === lo ? (top + bottom) / 2 : bottom - ((v - lo) / (hi - lo)) * (bottom - top))
        let dPath = ''
        let pen = false
        ys.forEach((v, k) => {
          if (v === null) { pen = false; return }
          dPath += `${pen ? 'L' : 'M'}${sx(k).toFixed(2)},${sy(v).toFixed(2)}`
          pen = true
        })
        const paint = delta ? toneP[delta.tone] === theme.muted ? theme.cat(0) : toneP[delta.tone] : theme.cat(0)
        marks.push({ type: 'path', key: `${key}:spark`, layer: 'labels', d: dPath, fill: 'none', stroke: paint, strokeWidth: 1.5, linejoin: 'round', linecap: 'round', inert: true })
        let lastK = ys.length - 1
        while (lastK > 0 && ys[lastK] === null) lastK--
        const lv = ys[lastK]
        if (lv !== null && lv !== undefined) marks.push({ type: 'circle', key: `${key}:spark-end`, layer: 'labels', cx: sx(lastK), cy: sy(lv), r: 2.5, fill: paint, inert: true })
      }
    }
    marks.push(...moreMarks(ctx, { ...grid, cellW: tileW, cellH: tileH }, 'tiles'))
    const nulls = items.filter((d) => d.value === null).length
    const zeroBase = items.filter((d) => d.value !== null && d.previous === 0 && d.value !== 0).length
    return {
      marks,
      note: notes([
        [nulls, nulls === 1 ? 'value missing (shown as —)' : 'values missing (shown as —)'],
        [zeroBase, zeroBase === 1 ? 'change from zero has no percentage' : 'changes from zero have no percentage'],
      ]),
    }
  },
  describe({ data, fmt, name, options: o }) {
    const one = (d: TileItem) => {
      const fv = tileFormatter(d, o)
      const v = d.value === null ? 'no value' : withUnit(fv.value(d.value), d.unit)
      const delta = kpiDelta(d.value, d.previous, d.goodDirection)
      if (!delta) return `${d.label} ${v}`
      if (delta.dir === 'flat') return `${d.label} ${v}, no change ${o.compareLabel}`
      return `${d.label} ${v}, ${delta.dir} ${deltaText(delta, fmt, d.unit, false, fv.change)} ${o.compareLabel}, ${delta.tone}`
    }
    if (data.length === 1) return `${name}, ${one(data[0]!)}`
    const bad = data.filter((d) => kpiDelta(d.value, d.previous, d.goodDirection)?.tone === 'bad').length
    return `${name}, ${phrase.count(data.length, 'tile')}: ${phrase.listPhrase(data.map(one), 2)}${bad ? `; ${bad} moving the wrong way` : ''}`
  },
  table(data) {
    return {
      columns: ['Label', 'Value', 'Previous', 'Change', 'Change %', 'Unit'],
      rows: data.map((d) => {
        const delta = kpiDelta(d.value, d.previous, d.goodDirection)
        return [d.label, d.value, d.previous, delta?.abs ?? null, delta?.pct ?? null, d.unit ?? null]
      }),
    }
  },
})

export default kpiTile
