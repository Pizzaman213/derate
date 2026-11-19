// Gauge: a semicircular dial from min to max. The arc fills to the value (or a needle
// points at it), coloured by the status band the value falls in; a target tick, the
// value centred under the arc, out-of-range values clamped with a note.

import { defineChart, phrase, SIZES, type Formatters, type Mark } from '../../core'
import { notes } from '../../core/helpers'
import { adaptObjects, arcPath, cellAt, fitGrid, moreMarks, polar, share, STATUS_PATTERN, statusPaint, uniqueKeys, withUnit, type Status } from '../_shared/KpiCharts'
import { input, options, type GaugeBand, type GaugeItem, type GaugeOptions } from './schema'

/** Needle/fill angle in radians, clockwise from 12 o'clock: -π/2 at min, +π/2 at max. */
export function gaugeAngle(value: number, min: number, max: number): number {
  return -Math.PI / 2 + Math.PI * share(value, min, max)
}

/** Bands as [from, to, status] ranges over [min, max], in order. */
export function bandRanges(bands: GaugeBand[] | undefined, min: number, max: number): Array<{ from: number; to: number; status: Status }> {
  if (!bands?.length) return []
  const sorted = [...bands].sort((a, b) => a.to - b.to)
  const out: Array<{ from: number; to: number; status: Status }> = []
  let from = min
  for (const b of sorted) {
    const to = Math.min(max, b.to)
    if (to > from) out.push({ from, to, status: b.status })
    from = Math.max(from, to)
  }
  return out
}

/** The status band a (clamped) value falls in, or null when no band covers it. */
export function gaugeStatus(item: Pick<GaugeItem, 'value' | 'min' | 'max' | 'bands'>): Status | null {
  if (item.value === null) return null
  const v = Math.min(item.max, Math.max(item.min, item.value))
  const r = bandRanges(item.bands, item.min, item.max)
  return r.find((b) => v >= b.from && v <= b.to)?.status ?? null
}

const MAX_R = 170

const nameOf = (d: GaugeItem, i: number, n: number) => d.label ?? (n > 1 ? `Gauge ${i + 1}` : 'Value')

function rangeNote(d: GaugeItem, i: number, n: number, fmt: Formatters): string | null {
  if (d.value === null) return null
  if (d.value > d.max) return `${nameOf(d, i, n)} is ${withUnit(fmt.value(d.value), d.unit)}, above the maximum ${withUnit(fmt.value(d.max), d.unit)}; the dial stops at the end`
  if (d.value < d.min) return `${nameOf(d, i, n)} is ${withUnit(fmt.value(d.value), d.unit)}, below the minimum ${withUnit(fmt.value(d.min), d.unit)}; the dial stops at the start`
  return null
}

export const gauge = defineChart<GaugeItem[], GaugeOptions>({
  id: 'gauge',
  name: 'Gauge',
  category: 'kpi',
  intents: ['kpi'],
  summary: 'A semicircular dial showing one value against its range, with status bands and a target.',
  input,
  options,
  shape: 'object',
  fields: ['value', 'min', 'max', 'target', 'label', 'unit'],
  adapt: adaptObjects,
  aspect: 0.6,
  minHeight: 150,
  maxHeight: 420,
  isEmpty: (data) => data.length === 0,
  draw(ctx) {
    const { theme, fmt, options: o, width: W, height: H } = ctx
    const items = ctx.data
    const n = items.length
    const anyLabel = items.some((d) => d.label)
    const labelH = anyLabel ? SIZES.label + 8 : 0
    const tickH = SIZES.tick + 6
    const pad = 6
    // a dial bigger than this only adds empty space around a fixed-size readout
    const radius = (cw: number, ch: number) => Math.min(MAX_R, cw / 2 - pad, ch - labelH - tickH - pad)
    const grid = fitGrid(n, W, H, { minW: 120, minH: 80, gap: 16, ratio: 0.62, score: radius })
    const keys = uniqueKeys(items.map((d, i) => d.label ?? `g${i + 1}`))
    const marks: Mark[] = []
    for (let i = 0; i < grid.shown; i++) {
      const d = items[i]!
      const key = keys[i]!
      const cell = cellAt(grid, i)
      const cw = grid.cellW, ch = grid.cellH
      const R = Math.max(12, radius(cw, ch))
      const thick = Math.max(6, Math.min(28, R * 0.24))
      const inner = R - thick
      const cx = cell.x + cw / 2
      const blockH = R + tickH + labelH
      const cy = cell.y + (ch - blockH) / 2 + R
      const shift = `translate(${cx},${cy})`
      const name = nameOf(d, i, n)
      const status = gaugeStatus(d)
      const bands = bandRanges(d.bands, d.min, d.max)
      const fmtU = (v: number) => withUnit(fmt.value(v), d.unit)
      const A0 = -Math.PI / 2, A1 = Math.PI / 2
      const needle = o.display === 'needle'
      // track: plain, or tinted by band (solid bands under a needle)
      const tint = needle ? 1 : 0.38
      if (bands.length && d.value !== null) {
        bands.forEach((b, j) => {
          const paint = statusPaint(theme, b.status)
          marks.push({
            type: 'path', key: `${key}:band:${j}`, layer: 'back', d: arcPath(inner, R, gaugeAngle(b.from, d.min, d.max), gaugeAngle(b.to, d.min, d.max)), transform: shift,
            fill: theme.mix(theme.track, paint, tint), pattern: STATUS_PATTERN[b.status], inert: true,
          })
        })
        // unbanded stretches stay plain track
        const covered = bands.reduce((s, b) => s + (b.to - b.from), 0)
        if (covered < d.max - d.min) {
          let from = d.min
          for (const b of [...bands, { from: d.max, to: d.max, status: 'good' as Status }]) {
            if (b.from > from) marks.push({ type: 'path', key: `${key}:gap:${from}`, layer: 'back', d: arcPath(inner, R, gaugeAngle(from, d.min, d.max), gaugeAngle(b.from, d.min, d.max)), transform: shift, fill: theme.track, inert: true })
            from = b.to
          }
        }
      } else if (d.value !== null) {
        marks.push({ type: 'path', key: `${key}:track`, layer: 'back', d: arcPath(inner, R, A0, A1), transform: shift, fill: theme.track, inert: true })
      }
      const tipRows: Array<[string, string]> = [['Value', d.value === null ? 'no value' : fmtU(d.value)], ['Range', `${fmtU(d.min)} – ${fmtU(d.max)}`]]
      if (d.target !== null) tipRows.push(['Target', fmtU(d.target)])
      if (status) tipRows.push(['Status', status])
      const statusText = status ? `, ${status}` : ''
      const targetText = d.target !== null ? `, target ${fmtU(d.target)}` : ''
      // the data mark is the whole dial (so hovering anywhere on it answers); what it
      // looks like is drawn above it, inert
      const hit = (label: string, cx: number, cy: number, color?: string): Mark => ({
        type: 'path', key: `${key}:value`, role: 'gauge', d: arcPath(0, R + 2, A0, A1), transform: shift, fill: 'transparent',
        cx, cy, box: { x: -R, y: -R, width: 2 * R, height: R },
        datum: d, label, tooltip: { title: name, rows: tipRows, color },
      })
      if (d.value === null) {
        marks.push(hit(`${name}: no value`, 0, -(inner + R) / 2))
        // an empty dial, dashed: missing is not zero
        marks.push({ type: 'path', key: `${key}:empty`, d: arcPath(inner, R, A0, A1), transform: shift, fill: 'none', stroke: theme.muted, strokeWidth: 1.5, dash: '4 3', inert: true })
      } else {
        const a = gaugeAngle(d.value, d.min, d.max)
        const paint = bands.length ? statusPaint(theme, status) : theme.cat(0)
        const [ex, ey] = polar((inner + R) / 2, a)
        marks.push(hit(`${name}: ${fmtU(d.value)} of ${fmtU(d.min)} – ${fmtU(d.max)}${statusText}${targetText}`, ex, ey, paint))
        if (needle) {
          const L = R + 2, w = Math.max(2, thick * 0.18)
          const [tx, ty] = polar(L, a), [lx, ly] = polar(w, a - Math.PI / 2), [rx, ry] = polar(w, a + Math.PI / 2)
          marks.push({ type: 'path', key: `${key}:needle`, d: `M${lx},${ly}L${tx},${ty}L${rx},${ry}Z`, transform: shift, fill: theme.ink, inert: true })
          marks.push({ type: 'circle', key: `${key}:hub`, cx: 0, cy: 0, r: w * 1.8, transform: shift, fill: theme.ink, inert: true })
        } else {
          const a1 = Math.max(a, A0 + 0.02)
          marks.push({ type: 'path', key: `${key}:fill`, d: arcPath(inner, R, A0, a1), transform: shift, fill: paint, pattern: status ? STATUS_PATTERN[status] : undefined, inert: true })
        }
      }
      // target tick
      if (d.target !== null) {
        const t = gaugeAngle(d.target, d.min, d.max)
        const [x1, y1] = polar(inner - 3, t), [x2, y2] = polar(R + 3, t)
        marks.push({ type: 'line', key: `${key}:target`, layer: 'labels', x1, y1, x2, y2, transform: shift, stroke: theme.ink, strokeWidth: 2.5, linecap: 'round', inert: true })
      }
      // min / max under the arc ends
      const endY = SIZES.tick + 3
      const endW = Math.max(0, R - inner / 2)
      const minT = ctx.truncate(fmt.value(d.min), endW * 1.6), maxT = ctx.truncate(fmt.value(d.max), endW * 1.6)
      marks.push({ type: 'text', key: `${key}:min`, layer: 'labels', x: -(inner + R) / 2, y: endY, text: minT, anchor: 'middle', baseline: 'alphabetic', size: SIZES.tick, fill: theme.muted, transform: shift, inert: true })
      marks.push({ type: 'text', key: `${key}:max`, layer: 'labels', x: (inner + R) / 2, y: endY, text: maxT, anchor: 'middle', baseline: 'alphabetic', size: SIZES.tick, fill: theme.muted, transform: shift, inert: true })
      // readout: the largest size whose top corners stay inside the inner radius
      const readout = d.value === null ? '—' : fmtU(d.value)
      const base = needle ? Math.max(2, thick * 0.18) * 1.8 + 6 : 2
      let size = SIZES.value
      const chord = (s: number) => 2 * Math.sqrt(Math.max(0, inner * inner - (base + s * 0.8) ** 2)) * 0.9
      while (size > SIZES.tick && ctx.measure(readout, size, 600) > chord(size)) size--
      const room = Math.max(10, chord(size))
      const shown = ctx.truncate(readout, room, size)
      marks.push({ type: 'text', key: `${key}:readout`, layer: 'labels', x: 0, y: -base, text: shown, full: shown !== readout ? readout : undefined, anchor: 'middle', baseline: 'alphabetic', size, weight: 600, fill: d.value === null ? theme.muted : theme.ink, halo: needle, transform: shift, inert: true })
      if (d.label) {
        const t = ctx.truncate(d.label, cw - 4, SIZES.label)
        marks.push({ type: 'text', key: `${key}:label`, layer: 'labels', x: 0, y: tickH + labelH - 4, text: t, full: t !== d.label ? d.label : undefined, anchor: 'middle', baseline: 'alphabetic', size: SIZES.label, fill: theme.inkSecondary, transform: shift })
      }
    }
    marks.push(...moreMarks(ctx, grid, 'gauges'))
    // notes cover every item, drawn or not, so they never depend on the layout
    const ranges = items.map((d, i) => rangeNote(d, i, n, fmt)).filter((t): t is string => t !== null)
    const nulls = items.filter((d) => d.value === null).length
    const rangeText = ranges.length === 1 ? ranges[0] : ranges.length > 1 ? `${ranges.length} values outside their range, clamped to the dial` : undefined
    const rest = notes([[nulls, nulls === 1 ? 'value missing (dashed dial)' : 'values missing (dashed dials)']])
    return { marks, note: [rangeText, rest].filter(Boolean).join('; ') || undefined }
  },
  describe({ data, fmt, name }) {
    if (data.length === 1) {
      const d = data[0]!
      const fmtU = (v: number) => withUnit(fmt.value(v), d.unit)
      if (d.value === null) return `${name}, ${d.label ?? 'value'}, no value`
      const s = gaugeStatus(d)
      const parts = [`${name}, ${d.label ?? 'value'} at ${fmtU(d.value)} on ${fmtU(d.min)} to ${fmtU(d.max)}`]
      if (s) parts.push(s)
      if (d.target !== null) parts.push(`target ${fmtU(d.target)}`)
      if (d.value > d.max) parts.push('above the maximum')
      if (d.value < d.min) parts.push('below the minimum')
      return parts.join(', ')
    }
    const items = data.map((d, i) => ({ label: nameOf(d, i, data.length), value: d.value === null ? null : share(d.value, d.min, d.max) }))
    const pctFmt = (v: number) => `${fmt.pct(v)} of range`
    const statuses = data.map(gaugeStatus)
    const bad = statuses.filter((s) => s === 'bad').length
    return `${name}, ${phrase.count(data.length, 'gauge')}, ${phrase.extremes(items, fmt, pctFmt)}${bad ? `, ${bad} in the bad band` : ''}`
  },
  table(data) {
    return {
      columns: ['Label', 'Value', 'Min', 'Max', 'Target', 'Unit', 'Status'],
      rows: data.map((d, i) => [nameOf(d, i, data.length), d.value, d.min, d.max, d.target, d.unit ?? null, gaugeStatus(d)]),
    }
  },
})

export default gauge
