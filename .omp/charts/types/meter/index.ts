// Meter: HTML <meter> semantics drawn as a segmented bar. [min, max] is cut at `low` and
// `high` into three zones; the zone holding `optimum` is the good one, its neighbours
// are suboptimum and the far zone is poor -- exactly the browser's classification. The
// track shows every zone tinted by its grade, the value bar is painted in its own grade,
// and the readout names the grade in words so colour is never the only channel.

import { defineChart, phrase, SIZES, type Formatters, type Mark } from '../../core'
import { notes } from '../../core/helpers'
import { adaptObjects, moreRow, rowLayout, share, STATUS_PATTERN, statusPaint, uniqueKeys, withUnit, type Status } from '../_shared/KpiCharts'
import { input, options, type MeterItem, type MeterOptions } from './schema'

export type Zone = 'optimum' | 'suboptimum' | 'poor'
export type Region = 'low' | 'mid' | 'high'

export interface MeterBounds { min: number; max: number; low: number; high: number; optimum: number }

/** The HTML defaults and clamps: low/high/optimum default to min/max/midpoint, all clamped into [min, max], low <= high. */
export function meterBounds(d: Pick<MeterItem, 'min' | 'max' | 'low' | 'high' | 'optimum'>): MeterBounds {
  const { min, max } = d
  const clamp = (v: number, a: number, b: number) => Math.min(b, Math.max(a, v))
  const low = clamp(d.low ?? min, min, max)
  const high = clamp(d.high ?? max, low, max)
  const optimum = clamp(d.optimum ?? (min + max) / 2, min, max)
  return { min, max, low, high, optimum }
}

/** Which region a (clamped) value is in: below low, low..high inclusive, above high. */
export function meterRegion(v: number, b: MeterBounds): Region {
  return v < b.low ? 'low' : v > b.high ? 'high' : 'mid'
}

/** The grade of a region, given where the optimum lies (HTML's rule). */
export function regionZone(r: Region, b: MeterBounds): Zone {
  const opt = meterRegion(b.optimum, b)
  if (r === opt) return 'optimum'
  if (opt === 'mid') return 'suboptimum'
  // optimum at one end: the middle is suboptimum, the far end is poor
  return r === 'mid' ? 'suboptimum' : 'poor'
}

/** The grade of the item's value, or null when it has none. */
export function meterZone(d: Pick<MeterItem, 'value' | 'min' | 'max' | 'low' | 'high' | 'optimum'>): Zone | null {
  if (d.value === null) return null
  const b = meterBounds(d)
  return regionZone(meterRegion(Math.min(b.max, Math.max(b.min, d.value)), b), b)
}

/** The non-empty zones in order along the bar. */
export function meterSegments(b: MeterBounds): Array<{ region: Region; from: number; to: number; zone: Zone }> {
  const all: Array<{ region: Region; from: number; to: number }> = [
    { region: 'low', from: b.min, to: b.low },
    { region: 'mid', from: b.low, to: b.high },
    { region: 'high', from: b.high, to: b.max },
  ]
  return all.filter((s) => s.to > s.from).map((s) => ({ ...s, zone: regionZone(s.region, b) }))
}

const ZONE_STATUS: Record<Zone, Status> = { optimum: 'good', suboptimum: 'warning', poor: 'bad' }

const nameOf = (d: MeterItem, i: number, n: number) => d.label ?? (n > 1 ? `Meter ${i + 1}` : 'Meter')

function readout(d: MeterItem, showZone: boolean, fmt: Formatters): string {
  if (d.value === null) return 'No data'
  const v = withUnit(fmt.value(d.value), d.unit)
  const z = meterZone(d)
  return showZone && z ? `${v} · ${z}` : v
}

export const meter = defineChart<MeterItem[], MeterOptions>({
  id: 'meter',
  name: 'Meter',
  category: 'kpi',
  intents: ['kpi'],
  summary: 'An HTML <meter>: a bar cut into low, middle and high zones, graded optimum, suboptimum or poor against an optimum.',
  input,
  options,
  shape: 'object',
  fields: ['value', 'min', 'max', 'low', 'high', 'optimum', 'label', 'unit'],
  adapt: adaptObjects,
  aspect: 0.3,
  minHeight: 140,
  maxHeight: 480,
  isEmpty: (data) => data.length === 0,
  draw(ctx) {
    const { theme, fmt, options: o } = ctx
    const items = ctx.data
    const n = items.length
    const keys = uniqueKeys(items.map((d, i) => d.label ?? `m${i + 1}`))
    const names = items.map((d, i) => nameOf(d, i, n))
    const texts = items.map((d) => readout(d, o.showZone, fmt))
    const anyLabel = items.some((d) => d.label)
    const L = rowLayout(ctx, anyLabel ? names : items.map(() => ''), texts, { labelSize: SIZES.label, valueSize: SIZES.label })
    const marks: Mark[] = []
    for (let i = 0; i < L.shown; i++) {
      const d = items[i]!, key = keys[i]!, name = names[i]!
      const b = meterBounds(d)
      const y0 = L.top + i * L.rowH
      const th = L.trackH
      const ty = L.stacked ? y0 + L.rowH - th - 4 : y0 + (L.rowH - th) / 2
      const textY = L.stacked ? ty - 6 : y0 + L.rowH / 2
      const tx = L.trackX, tw = L.trackW
      const xOf = (v: number) => tx + share(v, b.min, b.max) * tw
      const rx = Math.min(3, th / 2)
      const missing = d.value === null
      if (missing) {
        marks.push({ type: 'rect', key: `${key}:track`, layer: 'back', x: tx + 0.75, y: ty + 0.75, width: Math.max(0, tw - 1.5), height: Math.max(0, th - 1.5), rx, fill: 'none', stroke: theme.muted, strokeWidth: 1.5, dash: '4 3', inert: true })
      } else {
        // the zones, tinted by grade, with the grade's pattern so they survive greyscale
        for (const s of meterSegments(b)) {
          const st = ZONE_STATUS[s.zone]
          marks.push({ type: 'rect', key: `${key}:zone:${s.region}`, layer: 'back', x: xOf(s.from), y: ty, width: Math.max(0, xOf(s.to) - xOf(s.from)), height: th, fill: theme.mix(theme.track, statusPaint(theme, st), 0.3), inert: true })
        }
      }
      // zone boundaries: under the value bar, visible on the track around it
      for (const [k, v] of [['low', b.low], ['high', b.high]] as const) {
        if (v <= b.min || v >= b.max) continue
        const x = xOf(v)
        marks.push({ type: 'line', key: `${key}:edge:${k}`, layer: 'back', x1: x, x2: x, y1: ty, y2: ty + th, stroke: theme.surface, strokeWidth: 2, inert: true })
      }
      const zone = meterZone(d)
      const paint = zone ? statusPaint(theme, ZONE_STATUS[zone]) : theme.muted
      if (!missing) {
        const vh = Math.max(2, Math.round(th * 0.5))
        const w = share(d.value!, b.min, b.max) * tw
        if (w > 0) marks.push({ type: 'rect', key: `${key}:fill`, x: tx, y: ty + (th - vh) / 2, width: Math.max(1, w), height: vh, rx: Math.min(2, vh / 2), fill: paint, pattern: zone ? STATUS_PATTERN[ZONE_STATUS[zone]] : undefined, inert: true })
      }
      const tip: Array<[string, string]> = [
        ['Value', missing ? 'no data' : withUnit(fmt.value(d.value!), d.unit)],
        ...(zone ? [['Zone', zone] as [string, string]] : []),
        ['Range', `${fmt.value(b.min)} – ${fmt.value(b.max)}`],
        ['Low / high', `${fmt.value(b.low)} / ${fmt.value(b.high)}`],
        ['Optimum', fmt.value(b.optimum)],
      ]
      marks.push({
        type: 'rect', role: 'meter', key: `${key}:value`, x: tx, y: ty - 3, width: tw, height: th + 6, fill: 'transparent',
        datum: d, label: missing ? `${name}: no data` : `${name}: ${withUnit(fmt.value(d.value!), d.unit)}, ${zone}`, tooltip: { title: name, rows: tip, color: paint },
      })
      if (anyLabel) {
        const lt = ctx.truncate(name, L.labelW, SIZES.label)
        if (lt) marks.push({ type: 'text', key: `${key}:label`, layer: 'labels', x: L.labelX, y: textY, text: lt, full: lt !== name ? name : undefined, anchor: 'start', baseline: L.stacked ? 'alphabetic' : 'central', size: SIZES.label, fill: theme.inkSecondary })
      }
      const t = texts[i]!
      const tt = ctx.truncate(t, L.valueW + (L.stacked ? 0 : 4), SIZES.label)
      marks.push({ type: 'text', key: `${key}:readout`, layer: 'labels', x: L.valueX, y: textY, text: tt, full: tt !== t ? t : undefined, anchor: 'end', baseline: L.stacked ? 'alphabetic' : 'central', size: SIZES.label, weight: missing ? 400 : 600, fill: missing ? theme.muted : theme.ink })
    }
    marks.push(...moreRow(ctx, L, 'meters'))
    const nulls = items.filter((d) => d.value === null).length
    const out = items.filter((d) => d.value !== null && (d.value < d.min || d.value > d.max)).length
    return {
      marks,
      note: notes([
        [nulls, nulls === 1 ? 'value missing (dashed track)' : 'values missing (dashed tracks)'],
        [out, out === 1 ? 'value outside its range, drawn at the end' : 'values outside their range, drawn at the end'],
      ]),
    }
  },
  describe({ data, fmt, name }) {
    if (data.length === 1) {
      const d = data[0]!
      if (d.value === null) return `${name}, ${d.label ?? 'value'}, no data`
      return `${name}, ${d.label ?? 'value'} at ${withUnit(fmt.value(d.value), d.unit)} of ${fmt.value(d.min)} to ${fmt.value(d.max)}, ${meterZone(d)}`
    }
    const zones = data.map(meterZone)
    const count = (z: Zone) => zones.filter((x) => x === z).length
    const parts = (['optimum', 'suboptimum', 'poor'] as const).filter((z) => count(z) > 0).map((z) => `${count(z)} ${z}`)
    const missing = zones.filter((z) => z === null).length
    if (missing) parts.push(`${missing} with no data`)
    return `${name}, ${phrase.count(data.length, 'meter')}, ${parts.join(', ')}`
  },
  table(data) {
    return {
      columns: ['Label', 'Value', 'Min', 'Max', 'Low', 'High', 'Optimum', 'Zone'],
      rows: data.map((d, i) => { const b = meterBounds(d); return [nameOf(d, i, data.length), d.value, b.min, b.max, b.low, b.high, b.optimum, meterZone(d)] }),
    }
  },
})

export default meter
