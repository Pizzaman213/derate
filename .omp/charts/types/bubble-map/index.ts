// Bubble map: proportional symbols on a base map. Area is proportional to the value
// (radius ~ sqrt); bubbles sit at a region's centroid or at an explicit lon/lat.

import { defineChart, phrase, SIZES } from '../../core'
import { notes } from '../../core/helpers'
import type { Formatters, Mark, Palette } from '../../core'
import { baseMap, fitMap, lonLatText, nameList, regionAnchor, resolveKey, unmatchedNote, validLonLat, type MapFrame } from '../_shared/GeoCharts'
import { input, options, type BubbleMapOptions, type BubbleMapRow } from './schema'

interface Item {
  key: string
  name: string
  value: number
  region?: string
  lon?: number
  lat?: number
  /** Region feature anchor or lon/lat, resolved per frame. */
  at: (frame: MapFrame) => [number, number] | null
}

/** Rows -> bubbles (regions summed), plus what was left out and why. */
export function collectBubbles(data: BubbleMapRow[], o: BubbleMapOptions) {
  const items: Item[] = []
  const byRegion = new Map<string, Item>()
  const unmatched: string[] = []
  let noLocation = 0, nulls = 0, negatives = 0, duplicates = 0
  data.forEach((r, i) => {
    if (r.value === null) { nulls++; return }
    if (r.value < 0) { negatives++; return }
    if (r.region !== undefined) {
      const res = resolveKey(o.map, r.region)
      if (!res) { unmatched.push(r.region); return }
      const hit = byRegion.get(res.region.key)
      if (hit) { hit.value += r.value; duplicates++; return }
      const f = res.region.feature
      const item: Item = { key: `r:${res.region.key}`, name: r.label ?? res.display, value: r.value, region: res.region.name, at: (frame) => regionAnchor(frame, f) }
      byRegion.set(res.region.key, item)
      items.push(item)
      return
    }
    const ll = validLonLat(r.lon, r.lat)
    if (!ll) { noLocation++; return }
    items.push({ key: `p:${i}`, name: r.label ?? lonLatText(ll[0], ll[1]), value: r.value, lon: ll[0], lat: ll[1], at: (frame) => frame.project(ll[0], ll[1]) })
  })
  return { items, unmatched, noLocation, nulls, negatives, duplicates }
}

/** 1, 2 or 5 times a power of ten, at most v. */
const niceBelow = (v: number) => {
  const p = 10 ** Math.floor(Math.log10(v))
  return [5, 2, 1].map((m) => m * p).find((x) => x <= v) ?? p
}

/** Nested-circle size key in a bottom corner: the largest value and a smaller round one. */
function sizeKey(ctx: { width: number; height: number; theme: Palette; fmt: Formatters; measure: (t: string, s?: number) => number }, rMax: number, max: number, right: boolean): Mark[] {
  const { theme, fmt } = ctx
  const pad = 4
  const cx = right ? ctx.width - pad - rMax : pad + rMax
  const yb = ctx.height - pad
  const small = niceBelow(max / 4)
  const steps = [max, small].filter((v, i, a) => v > 0 && (i === 0 || 2 * rMax * (1 - Math.sqrt(v / max)) >= SIZES.tick + 2) && a.indexOf(v) === i)
  const marks: Mark[] = []
  for (const v of steps) {
    const r = rMax * Math.sqrt(v / max)
    const top = yb - 2 * r
    const tx = right ? cx - rMax - 6 : cx + rMax + 6
    marks.push({ type: 'circle', layer: 'labels', key: `key:c:${v}`, cx, cy: yb - r, r, fill: 'none', stroke: theme.muted, strokeWidth: 1, inert: true })
    marks.push({ type: 'line', layer: 'labels', key: `key:l:${v}`, x1: cx, y1: top, x2: right ? tx + 3 : tx - 3, y2: top, stroke: theme.muted, strokeWidth: 0.75, dash: '2 2', inert: true })
    marks.push({ type: 'text', layer: 'labels', key: `key:t:${v}`, x: tx, y: Math.max(top, SIZES.tick / 2 + 1), text: fmt.value(v), anchor: right ? 'end' : 'start', baseline: 'middle', size: SIZES.tick, fill: ctx.theme.inkSecondary, halo: true, inert: true })
  }
  return marks
}

export const bubbleMap = defineChart<BubbleMapRow[], BubbleMapOptions>({
  id: 'bubble-map',
  name: 'Bubble map',
  category: 'geo',
  intents: ['geo'],
  summary: 'Circles sized by a value on a world or US map.',
  input,
  options,
  shape: 'rows',
  fields: ['region', 'lon', 'lat', 'value', 'label'],
  numberFields: ['lon', 'lat', 'value'],
  aspect: 0.56,
  minHeight: 170,
  draw(ctx) {
    const { theme, options: o, fmt } = ctx
    const frame = fitMap(o.map, ctx.width, ctx.height)
    const { items, unmatched, noLocation, nulls, negatives, duplicates } = collectBubbles(ctx.data, o)
    const max = Math.max(0, ...items.map((d) => d.value))
    const rMax = o.maxRadius ?? Math.min(36, Math.max(6, Math.min(ctx.width, ctx.height) * 0.07))
    const radius = (v: number) => (max > 0 ? Math.max(1.5, rMax * Math.sqrt(v / max)) : 1.5)
    const marks: Mark[] = baseMap(frame, theme)
    const placed: Array<{ d: Item; x: number; y: number; r: number }> = []
    const offMap: string[] = []
    for (const d of items) {
      const p = d.at(frame)
      if (!p) { offMap.push(d.name); continue }
      placed.push({ d, x: p[0], y: p[1], r: radius(d.value) })
    }
    // big first, so small bubbles stay on top and hoverable
    placed.sort((a, b) => b.r - a.r)
    const fill = theme.cat(0)
    for (const { d, x, y, r } of placed) {
      const v = fmt.value(d.value)
      const rows: Array<[string, string]> = [[o.valueLabel ?? 'Value', v]]
      if (d.region && d.region !== d.name) rows.push(['Region', d.region])
      if (d.lon !== undefined && d.lat !== undefined) rows.push(['Location', lonLatText(d.lon, d.lat)])
      marks.push({
        type: 'circle', role: 'bubble', key: d.key, cx: x, cy: y, r, fill, fillOpacity: 0.55, stroke: fill, strokeWidth: 1,
        datum: { name: d.name, value: d.value, region: d.region, lon: d.lon, lat: d.lat },
        label: `${d.name}: ${v}`, tooltip: { title: d.name, rows, color: fill },
      })
    }
    if (o.showLabels) {
      const taken: Array<[number, number, number, number]> = []
      for (const { d, x, y, r } of placed.slice(0, 24)) {
        const w = ctx.measure(d.name, SIZES.tick)
        if (w > 2 * r - 6 || SIZES.tick > r) continue
        const box: [number, number, number, number] = [x - w / 2, y - SIZES.tick / 2, x + w / 2, y + SIZES.tick / 2]
        if (taken.some((t) => box[0] < t[2] && box[2] > t[0] && box[1] < t[3] && box[3] > t[1])) continue
        taken.push(box)
        marks.push({ type: 'text', layer: 'labels', key: `lbl:${d.key}`, x, y, text: d.name, anchor: 'middle', baseline: 'central', size: SIZES.tick, fill: theme.ink, halo: true, inert: true })
      }
    }
    if (max > 0 && placed.length) marks.push(...sizeKey(ctx, rMax, max, o.map === 'us-states'))
    return {
      marks,
      note: [
        unmatchedNote(unmatched),
        offMap.length ? `${offMap.length} outside this map: ${nameList(offMap)}` : undefined,
        notes([[duplicates, 'duplicate regions summed'], [nulls, 'missing values skipped'], [negatives, 'negative values omitted'], [noLocation, 'rows without a region or a valid lon/lat skipped']]),
      ].filter(Boolean).join('; ') || undefined,
    }
  },
  describe({ data, options: o, fmt, name }) {
    const { items, unmatched } = collectBubbles(data, o)
    const base = `${name}, ${phrase.count(items.length, 'bubble')}${o.valueLabel ? ` sized by ${o.valueLabel}` : ''}, ${phrase.extremes(items.map((d) => ({ label: d.name, value: d.value })), fmt)}`
    return unmatched.length ? `${base}, ${unmatched.length} not found` : base
  },
  table(data) {
    return {
      columns: ['Region', 'Longitude', 'Latitude', 'Label', 'Value'],
      rows: data.map((r) => [r.region ?? null, r.lon, r.lat, r.label ?? null, r.value]),
    }
  },
})

export default bubbleMap
