// Progress ring: a circle that fills clockwise from 12 o'clock as value approaches max,
// with the percentage in the middle. Several rings sit side by side, or nest
// concentrically with a key beside them. Past 100% a second, darker lap shows the
// overshoot; a missing value is a dashed empty ring, never a zero.

import { defineChart, phrase, SIZES, type Formatters, type Mark, type Palette } from '../../core'
import { notes } from '../../core/helpers'
import { adaptObjects, arcPath, cellAt, fitGrid, moreMarks, uniqueKeys } from '../_shared/KpiCharts'
import { input, options, type RingItem, type RingOptions } from './schema'

const TAU = Math.PI * 2
const MAX_R = 110

/** value / max (negative clamps to 0; may exceed 1), or null when missing. */
export function ringFraction(d: Pick<RingItem, 'value' | 'max'>): number | null {
  return d.value === null ? null : Math.max(0, d.value / d.max)
}

/** "82%", "4.5%" (one decimal under 10%), "—" when missing. */
export function ringPercent(f: number | null, fmt: Formatters): string {
  if (f === null) return '—'
  return fmt.pct(f, f > 0 && f < 0.1 ? 1 : 0)
}

const nameOf = (d: RingItem, i: number, n: number) => d.label ?? (n > 1 ? `Ring ${i + 1}` : 'Progress')

/** The filled arc(s) of a ring: the first lap, and the overshoot lap past 100%. */
function laps(key: string, f: number, inner: number, outer: number, paint: string, theme: Palette, shift: string, pattern: number | undefined): Mark[] {
  const corner = (outer - inner) / 2
  const first = Math.min(1, f)
  const out: Mark[] = []
  if (first > 0) out.push({ type: 'path', key: `${key}:lap1`, d: arcPath(inner, outer, 0, Math.max(first * TAU, 0.001), first < 1 ? corner : 0), transform: shift, fill: paint, pattern, inert: true })
  else out.push({ type: 'path', key: `${key}:lap1`, d: arcPath(inner, outer, -0.01, 0.01), transform: shift, fill: paint, inert: true })
  if (f > 1) {
    const over = Math.min(1, f - 1)
    out.push({ type: 'path', key: `${key}:lap2`, d: arcPath(inner, outer, 0, Math.max(over * TAU, 0.001), over < 1 ? corner : 0), transform: shift, fill: theme.mix(paint, theme.ink, 0.4), inert: true })
  }
  return out
}

export const progressRing = defineChart<RingItem[], RingOptions>({
  id: 'progress-ring',
  name: 'Progress ring',
  category: 'kpi',
  intents: ['kpi'],
  summary: 'Rings that fill clockwise toward a goal, with the percentage in the middle.',
  input,
  options,
  shape: 'object',
  fields: ['value', 'max', 'label'],
  adapt: adaptObjects,
  aspect: 0.5,
  minHeight: 140,
  maxHeight: 320,
  isEmpty: (data) => data.length === 0,
  draw(ctx) {
    const { theme, fmt, options: o, width: W, height: H } = ctx
    const items = ctx.data
    const n = items.length
    const keys = uniqueKeys(items.map((d, i) => d.label ?? `r${i + 1}`))
    const marks: Mark[] = []
    const tipFor = (d: RingItem, i: number, f: number | null): { label: string; rows: Array<[string, string]> } => {
      const name = nameOf(d, i, n)
      if (f === null) return { label: `${name}: no value`, rows: [['Value', 'no value'], ['Goal', fmt.value(d.max)]] }
      return { label: `${name}: ${ringPercent(f, fmt)} (${fmt.value(d.value)} of ${fmt.value(d.max)})`, rows: [['Progress', ringPercent(f, fmt)], ['Value', fmt.value(d.value)], ['Goal', fmt.value(d.max)]] }
    }

    if (o.layout === 'concentric' && n > 1) {
      // rings on the left (or top), a key with each value beside them
      const keyW = Math.min(W * 0.5, Math.max(...items.map((d, i) => ctx.measure(`${nameOf(d, i, n)}  100%`, SIZES.label))) + 24)
      const side = W - keyW - 16 >= 120
      const R = Math.max(24, Math.min(MAX_R * 1.4, side ? Math.min(H / 2 - 4, (W - keyW - 16) / 2) : Math.min(W / 2 - 4, (H - 4) / 2 - n * 9)))
      const gap = 3
      const thick = Math.max(6, Math.min(20, (R - 14) / n - gap))
      const fit = Math.max(1, Math.floor((R - 12 + gap) / (thick + gap)))
      const shown = Math.min(n, fit)
      const cx = side ? R + 4 : W / 2
      const cy = side ? H / 2 : R + 4
      const shift = `translate(${cx},${cy})`
      const keyX = side ? cx + R + 20 : 4
      const rowH = SIZES.label + 8
      const keyTop = side ? cy - (shown * rowH) / 2 : cy + R + 10
      for (let i = 0; i < shown; i++) {
        const d = items[i]!, key = keys[i]!
        const outer = R - i * (thick + gap), inner = outer - thick
        const f = ringFraction(d)
        const paint = theme.cat(i)
        const t = tipFor(d, i, f)
        marks.push({ type: 'path', key: `${key}:track`, layer: 'back', d: arcPath(inner, outer, 0, TAU), transform: shift, fill: f === null ? 'none' : theme.track, stroke: f === null ? theme.muted : undefined, strokeWidth: f === null ? 1.5 : undefined, dash: f === null ? '4 3' : undefined, inert: true })
        marks.push({
          type: 'path', key: `${key}:value`, role: 'ring', series: key, d: arcPath(inner - gap / 2, outer + gap / 2, 0, TAU), transform: shift, fill: 'transparent',
          cx: 0, cy: -(inner + outer) / 2, box: { x: -outer, y: -outer, width: 2 * outer, height: 2 * outer },
          datum: d, label: t.label, tooltip: { title: nameOf(d, i, n), rows: t.rows, color: paint },
        })
        if (f !== null) marks.push(...laps(key, f, inner, outer, paint, theme, shift, i))
        // key row
        const y = keyTop + i * rowH + rowH / 2
        const pctText = ringPercent(f, fmt)
        const pctW = ctx.measure(pctText, SIZES.label, 600)
        const avail = (side ? W - keyX : W - 8) - 16 - pctW - 8
        const name = nameOf(d, i, n)
        const lt = ctx.truncate(name, avail, SIZES.label)
        marks.push({ type: 'rect', key: `${key}:sw`, layer: 'labels', x: keyX, y: y - 5, width: 10, height: 10, rx: 2, fill: paint, pattern: i, inert: true })
        marks.push({ type: 'text', key: `${key}:name`, layer: 'labels', x: keyX + 16, y, text: lt, full: lt !== name ? name : undefined, anchor: 'start', baseline: 'central', size: SIZES.label, fill: theme.inkSecondary })
        marks.push({ type: 'text', key: `${key}:pct`, layer: 'labels', x: keyX + 16 + Math.min(ctx.measure(lt, SIZES.label), avail) + 8, y, text: pctText, anchor: 'start', baseline: 'central', size: SIZES.label, weight: 600, fill: f === null ? theme.muted : theme.ink, inert: true })
      }
      if (shown < n) {
        const t = ctx.truncate(`+${n - shown} more (in the data table)`, side ? W - keyX : W - 8, SIZES.tick)
        marks.push({ type: 'text', key: 'more', layer: 'labels', x: keyX + 16, y: keyTop + shown * rowH + rowH / 2, text: t, anchor: 'start', baseline: 'central', size: SIZES.tick, fill: theme.muted, inert: true })
      }
    } else {
      const anyLabel = items.some((d) => d.label)
      const labelH = anyLabel ? SIZES.label + 10 : 0
      const radius = (cw: number, ch: number) => Math.min(MAX_R, cw / 2 - 4, (ch - labelH) / 2 - 4)
      const grid = fitGrid(n, W, H, { minW: 64, minH: 64, gap: 12, ratio: 1, score: radius })
      for (let i = 0; i < grid.shown; i++) {
        const d = items[i]!, key = keys[i]!
        const cell = cellAt(grid, i)
        const R = Math.max(14, radius(grid.cellW, grid.cellH))
        const thick = Math.max(5, Math.min(18, R * 0.2))
        const inner = R - thick
        const cx = cell.x + grid.cellW / 2
        const cy = cell.y + (grid.cellH - labelH) / 2
        const shift = `translate(${cx},${cy})`
        const f = ringFraction(d)
        const paint = theme.cat(0)
        const t = tipFor(d, i, f)
        marks.push({ type: 'path', key: `${key}:track`, layer: 'back', d: arcPath(inner, R, 0, TAU), transform: shift, fill: f === null ? 'none' : theme.track, stroke: f === null ? theme.muted : undefined, strokeWidth: f === null ? 1.5 : undefined, dash: f === null ? '4 3' : undefined, inert: true })
        // the whole disc answers hover; the arcs are drawn above it, inert
        marks.push({
          type: 'path', key: `${key}:value`, role: 'ring', d: arcPath(0, R + 2, 0, TAU), transform: shift, fill: 'transparent',
          cx: 0, cy: -(inner + R) / 2, box: { x: -R, y: -R, width: 2 * R, height: 2 * R },
          datum: d, label: t.label, tooltip: { title: nameOf(d, i, n), rows: t.rows, color: paint },
        })
        if (f !== null) marks.push(...laps(key, f, inner, R, paint, theme, shift, undefined))
        const text = ringPercent(f, fmt)
        let size = Math.min(SIZES.value, Math.max(SIZES.tick, Math.floor(inner * 0.55)))
        while (size > SIZES.tick && ctx.measure(text, size, 600) > inner * 1.6) size--
        const shownText = ctx.truncate(text, inner * 1.7, size)
        marks.push({ type: 'text', key: `${key}:pct`, layer: 'labels', x: 0, y: 0, text: shownText, full: shownText !== text ? text : undefined, anchor: 'middle', baseline: 'central', size, weight: 600, fill: f === null ? theme.muted : theme.ink, transform: shift, inert: true })
        if (d.label) {
          const lt = ctx.truncate(d.label, grid.cellW - 4, SIZES.label)
          marks.push({ type: 'text', key: `${key}:label`, layer: 'labels', x: 0, y: R + 8, text: lt, full: lt !== d.label ? d.label : undefined, anchor: 'middle', baseline: 'hanging', size: SIZES.label, fill: theme.inkSecondary, transform: shift })
        }
      }
      marks.push(...moreMarks(ctx, grid, 'rings'))
    }
    const nulls = items.filter((d) => d.value === null).length
    const negatives = items.filter((d) => d.value !== null && d.value < 0).length
    const over = items.filter((d) => d.value !== null && d.value > d.max).length
    return {
      marks,
      note: notes([
        [nulls, nulls === 1 ? 'value missing (dashed ring)' : 'values missing (dashed rings)'],
        [negatives, negatives === 1 ? 'negative value shown as 0%' : 'negative values shown as 0%'],
        [over, over === 1 ? 'ring past 100% (the darker second lap is the overshoot)' : 'rings past 100% (the darker second lap is the overshoot)'],
      ]),
    }
  },
  describe({ data, fmt, name }) {
    if (data.length === 1) {
      const d = data[0]!
      const f = ringFraction(d)
      return f === null ? `${name}, ${d.label ?? 'progress'}, no value` : `${name}, ${d.label ?? 'progress'} at ${ringPercent(f, fmt)}, ${fmt.value(d.value)} of ${fmt.value(d.max)}`
    }
    const items = data.map((d, i) => ({ label: nameOf(d, i, data.length), value: ringFraction(d) }))
    const done = items.filter((d) => d.value !== null && d.value >= 1).length
    return `${name}, ${phrase.count(data.length, 'ring')}, ${phrase.extremes(items, fmt, (v) => ringPercent(v, fmt))}${done ? `, ${done} complete` : ''}`
  },
  table(data) {
    return {
      columns: ['Label', 'Value', 'Max', 'Fraction'],
      rows: data.map((d, i) => [nameOf(d, i, data.length), d.value, d.max, ringFraction(d)]),
    }
  },
})

export default progressRing
