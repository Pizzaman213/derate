// Flow map: arcs between places on a base map, width proportional to the value, an
// arrowhead at the destination. Curved arcs bow to the left of travel so A->B and B->A
// stay apart; great-circle arcs follow the shortest route (d3-geo geoInterpolate).

import { geoInterpolate } from 'd3-geo'
import { defineChart, phrase, SIZES } from '../../core'
import { notes } from '../../core/helpers'
import type { LegendSpec, Mark, Rect } from '../../core'
import { baseMap, fitMap, lonLatText, nameList, regionLonLat, resolveKey, unmatchedNote, validLonLat, type MapFrame } from '../_shared/GeoCharts'
import { input, options, type FlowMapOptions, type FlowMapRow } from './schema'

/** `named`: a region, so worth a label; bare coordinates are not. */
interface Place { id: string; name: string; named: boolean; lonLat: [number, number] }
export interface Flow { key: string; from: Place; to: Place; value: number }

/** Narrowest pointer target (px) for a flow; thicker flows are hit on their own width. */
const HIT_WIDTH = 8

/** Rows -> flows (same from/to summed), plus what was left out. */
export function collectFlows(data: FlowMapRow[], o: FlowMapOptions) {
  const unmatched: string[] = []
  let nulls = 0, negatives = 0, noLocation = 0, self = 0, duplicates = 0
  const end = (key: string | undefined, lon: number | null, lat: number | null): Place | null | 'unmatched' => {
    if (key !== undefined) {
      const res = resolveKey(o.map, key)
      if (!res) { unmatched.push(key); return 'unmatched' }
      return { id: res.region.key, name: res.display, named: true, lonLat: regionLonLat(res.region.feature) }
    }
    const ll = validLonLat(lon, lat)
    return ll ? { id: `${ll[0]},${ll[1]}`, name: lonLatText(ll[0], ll[1]), named: false, lonLat: ll } : null
  }
  const flows = new Map<string, Flow>()
  for (const r of data) {
    const a = end(r.from, r.fromLon, r.fromLat), b = end(r.to, r.toLon, r.toLat)
    if (a === 'unmatched' || b === 'unmatched') continue
    if (!a || !b) { noLocation++; continue }
    if (r.value === null) { nulls++; continue }
    if (r.value < 0) { negatives++; continue }
    if (a.id === b.id) { self++; continue }
    const key = `${a.id}>${b.id}`
    const hit = flows.get(key)
    if (hit) { hit.value += r.value; duplicates++; continue }
    flows.set(key, { key, from: a, to: b, value: r.value })
  }
  return { flows: [...flows.values()], unmatched, nulls, negatives, noLocation, self, duplicates }
}

type Pt = [number, number]
const r1 = (v: number) => Math.round(v * 10) / 10

/** Screen polyline(s) of a great-circle route; split where the projection jumps (antimeridian) or drops points. */
function greatCircle(frame: MapFrame, a: Pt, b: Pt): Pt[][] {
  const interp = geoInterpolate(a, b)
  const parts: Pt[][] = [[]]
  const jump = frame.box.width / 3
  let prev: Pt | null = null
  for (let i = 0; i <= 48; i++) {
    const ll = interp(i / 48)
    const p = frame.project(ll[0], ll[1])
    if (!p || (prev && Math.abs(p[0] - prev[0]) > jump)) { if (parts[parts.length - 1]!.length) parts.push([]); prev = p; if (!p) continue }
    parts[parts.length - 1]!.push(p)
    prev = p
  }
  return parts.filter((s) => s.length > 1)
}

/** Shift a polyline `by` px to the left of travel (same side the curved arcs bow to). */
function offsetLeft(s: Pt[], by: number): Pt[] {
  return s.map((p, i) => {
    const a = s[Math.max(0, i - 1)]!, b = s[Math.min(s.length - 1, i + 1)]!
    const dx = b[0] - a[0], dy = b[1] - a[1]
    const l = Math.hypot(dx, dy) || 1
    return [p[0] + (dy / l) * by, p[1] - (dx / l) * by]
  })
}

/** Filled arrowhead with its tip at `tip`, pointing along `dir`. */
function arrowHead(tip: Pt, dir: Pt, len: number, half: number): string {
  const l = Math.hypot(dir[0], dir[1]) || 1
  const ux = dir[0] / l, uy = dir[1] / l
  const bx = tip[0] - ux * len, by = tip[1] - uy * len
  return `M${r1(tip[0])},${r1(tip[1])}L${r1(bx - uy * half)},${r1(by + ux * half)}L${r1(bx + uy * half)},${r1(by - ux * half)}Z`
}

export const flowMap = defineChart<FlowMapRow[], FlowMapOptions>({
  id: 'flow-map',
  name: 'Flow map',
  category: 'geo',
  intents: ['geo', 'flow'],
  summary: 'Arcs between places on a map, width proportional to the volume moved.',
  input,
  options,
  shape: 'rows',
  fields: ['from', 'to', 'fromLon', 'fromLat', 'toLon', 'toLat', 'value'],
  numberFields: ['fromLon', 'fromLat', 'toLon', 'toLat', 'value'],
  aspect: 0.56,
  minHeight: 170,
  draw(ctx) {
    const { theme, options: o, fmt } = ctx
    const frame = fitMap(o.map, ctx.width, ctx.height)
    const c = collectFlows(ctx.data, o)
    const max = Math.max(0, ...c.flows.map((f) => f.value))
    // dense maps: thinner, fainter lines and no arrowheads, or they merge into one blot
    const n = c.flows.length
    const maxW = o.maxWidth ?? Math.max(1.5, Math.min(12, Math.max(3, Math.min(ctx.width, ctx.height) * 0.025)) * Math.min(1, Math.sqrt(60 / Math.max(1, n))))
    const opacity = n > 500 ? 0.3 : n > 100 ? 0.55 : 0.75
    const withArrows = n <= 1000
    const minW = Math.min(1, maxW)
    const width = (v: number) => (max > 0 ? minW + ((maxW - minW) * v) / max : minW)
    const origins = [...new Set(c.flows.map((f) => f.from.name))]
    const colorOf = new Map(origins.map((n, i) => [n, o.colorBy === 'origin' ? theme.cat(i) : theme.cat(0)]))
    const marks: Mark[] = baseMap(frame, theme)
    const arrows: Mark[] = []
    const hits: Mark[] = []
    const offMap: string[] = []
    const places = new Map<string, { p: Pt; name: string; named: boolean; total: number }>()
    // thick first so thin flows stay on top and hoverable
    const ordered = [...c.flows].sort((a, b) => b.value - a.value)
    const routes = new Set(c.flows.map((f) => f.key))
    for (const f of ordered) {
      const a = frame.project(...f.from.lonLat), b = frame.project(...f.to.lonLat)
      if (!a || !b) { offMap.push(`${f.from.name} → ${f.to.name}`); continue }
      const series = o.colorBy === 'origin' ? f.from.name : undefined
      const hidden = series !== undefined && ctx.hidden.has(series)
      for (const [pl, p] of [[f.from, a], [f.to, b]] as const) {
        const hit = places.get(pl.id)
        if (hit) hit.total += hidden ? 0 : f.value
        else places.set(pl.id, { p, name: pl.name, named: pl.named, total: hidden ? 0 : f.value })
      }
      if (hidden) continue
      const w = width(f.value)
      const color = colorOf.get(f.from.name)!
      const head = withArrows ? Math.max(5, w * 1.6) : 0, half = Math.max(3, w * 1.1)
      let d: string, hitD: string, mid: Pt, tip: Pt, dir: Pt, box: Rect
      if (o.arc === 'great-circle') {
        // A->B and B->A share one great circle: pull each to its own side so both show and both can be hovered
        const raw = greatCircle(frame, f.from.lonLat, f.to.lonLat)
        const parts = routes.has(`${f.to.id}>${f.from.id}`) ? raw.map((s) => offsetLeft(s, Math.max(w / 2 + 1, HIT_WIDTH / 2))) : raw
        if (!parts.length) { offMap.push(`${f.from.name} → ${f.to.name}`); continue }
        const last = parts[parts.length - 1]!
        tip = last[last.length - 1]!
        const before = last[Math.max(0, last.length - 3)]!
        dir = [tip[0] - before[0], tip[1] - before[1]]
        // pull the line back under the arrowhead so its end does not poke through the tip
        const l = Math.hypot(dir[0], dir[1]) || 1
        last[last.length - 1] = [tip[0] - (dir[0] / l) * head * 0.8, tip[1] - (dir[1] / l) * head * 0.8]
        const line = (s: Pt[]) => s.map((p, i) => `${i ? 'L' : 'M'}${r1(p[0])},${r1(p[1])}`).join('')
        d = parts.map(line).join('')
        const all = parts.flat()
        const longest = parts.reduce((m, s) => (s.length > m.length ? s : m), parts[0]!)
        mid = longest[Math.floor(longest.length / 2)]!
        // an arc split at the antimeridian is hovered on its longest piece: with both pieces
        // the hit box spans the whole map and is almost all empty space
        hitD = line(longest)
        const xs = all.map((p) => p[0]), ys = all.map((p) => p[1])
        box = { x: Math.min(...xs), y: Math.min(...ys), width: Math.max(1, Math.max(...xs) - Math.min(...xs)), height: Math.max(1, Math.max(...ys) - Math.min(...ys)) }
      } else {
        const dx = b[0] - a[0], dy = b[1] - a[1]
        // control point left of travel, a quarter of the chord out
        const k = 0.25
        const ctrl: Pt = [(a[0] + b[0]) / 2 + dy * k, (a[1] + b[1]) / 2 - dx * k]
        tip = b
        dir = [b[0] - ctrl[0], b[1] - ctrl[1]]
        const l = Math.hypot(dir[0], dir[1]) || 1
        const end: Pt = [b[0] - (dir[0] / l) * head * 0.8, b[1] - (dir[1] / l) * head * 0.8]
        d = hitD = `M${r1(a[0])},${r1(a[1])}Q${r1(ctrl[0])},${r1(ctrl[1])} ${r1(end[0])},${r1(end[1])}`
        mid = [0.25 * a[0] + 0.5 * ctrl[0] + 0.25 * b[0], 0.25 * a[1] + 0.5 * ctrl[1] + 0.25 * b[1]]
        const xs = [a[0], mid[0], b[0]], ys = [a[1], mid[1], b[1]]
        box = { x: Math.min(...xs), y: Math.min(...ys), width: Math.max(1, Math.max(...xs) - Math.min(...xs)), height: Math.max(1, Math.max(...ys) - Math.min(...ys)) }
      }
      const v = fmt.value(f.value)
      const title = `${f.from.name} → ${f.to.name}`
      // the visible line is a decoration; the data mark is the same path with a stroke at
      // least HIT_WIDTH wide and transparent, painted above every line and arrowhead, so a
      // 1 px flow can be hovered without pixel-hunting
      marks.push({ type: 'path', role: 'flow', key: `flow:${f.key}`, d, fill: 'none', stroke: color, strokeWidth: w, strokeOpacity: opacity, linecap: 'butt', series, inert: true })
      hits.push({
        type: 'path', role: 'flow', key: `hit:${f.key}`, d: hitD, fill: 'none', stroke: 'transparent', strokeWidth: Math.max(w, HIT_WIDTH), linecap: 'butt', series,
        cx: mid[0], cy: mid[1], box,
        datum: { from: f.from.name, to: f.to.name, value: f.value },
        label: `${title}: ${v}`, tooltip: { title, rows: [[o.valueLabel ?? 'Value', v]], color },
      })
      if (withArrows) arrows.push({ type: 'path', key: `arrow:${f.key}`, d: arrowHead(tip, dir, head, half), fill: color, fillOpacity: 0.9, stroke: 'none', inert: true })
    }
    marks.push(...arrows, ...hits)
    const nodes = [...places.entries()].sort((a, b) => b[1].total - a[1].total)
    for (const [id, pl] of nodes) marks.push({ type: 'circle', key: `node:${id}`, cx: pl.p[0], cy: pl.p[1], r: 2.5, fill: theme.ink, stroke: theme.surface, strokeWidth: 1, inert: true })
    if (o.showLabels) {
      const taken: Array<[number, number, number, number]> = []
      for (const [id, pl] of nodes.slice(0, 12)) {
        if (pl.total <= 0 || !pl.named) continue
        const text = ctx.truncate(pl.name, Math.max(40, ctx.width * 0.18), SIZES.tick)
        if (!text) continue
        const w = ctx.measure(text, SIZES.tick)
        const right = pl.p[0] + 5 + w <= ctx.width - 2
        const x = right ? pl.p[0] + 5 : pl.p[0] - 5
        if (!right && x - w < 2) continue
        const y = Math.min(ctx.height - SIZES.tick / 2 - 2, Math.max(SIZES.tick / 2 + 2, pl.p[1]))
        const b: [number, number, number, number] = right ? [x, y - SIZES.tick / 2, x + w, y + SIZES.tick / 2] : [x - w, y - SIZES.tick / 2, x, y + SIZES.tick / 2]
        if (taken.some((t) => b[0] < t[2] && b[2] > t[0] && b[1] < t[3] && b[3] > t[1])) continue
        taken.push(b)
        marks.push({ type: 'text', layer: 'labels', key: `place:${id}`, x, y, text, full: text !== pl.name ? pl.name : undefined, anchor: right ? 'start' : 'end', baseline: 'central', size: SIZES.tick, fill: theme.ink, halo: true, inert: text === pl.name })
      }
    }
    const legend: LegendSpec | undefined = o.colorBy === 'origin' && origins.length > 1 ? { title: 'From', items: origins.map((n) => ({ key: n, label: n, color: colorOf.get(n)!, shape: 'line' })) } : undefined
    return {
      marks,
      legend,
      note: [
        unmatchedNote(c.unmatched),
        offMap.length ? `${offMap.length} ${offMap.length === 1 ? 'flow' : 'flows'} outside this map: ${nameList(offMap, 3)}` : undefined,
        notes([[c.duplicates, 'duplicate flows summed'], [c.nulls, 'missing values skipped'], [c.negatives, 'negative values omitted'], [c.self, 'flows from a place to itself skipped'], [c.noLocation, 'rows without both ends located skipped']]),
      ].filter(Boolean).join('; ') || undefined,
    }
  },
  describe({ data, options: o, fmt, name }) {
    const { flows, unmatched } = collectFlows(data, o)
    const places = new Set(flows.flatMap((f) => [f.from.id, f.to.id])).size
    const top = flows.reduce<Flow | null>((m, f) => (!m || f.value > m.value ? f : m), null)
    const total = flows.reduce((s, f) => s + f.value, 0)
    const base = `${name}, ${phrase.count(flows.length, 'flow')} between ${phrase.count(places, 'place')}${top ? `, largest ${top.from.name} → ${top.to.name} at ${fmt.value(top.value)}, total ${fmt.value(total)}` : ''}`
    return unmatched.length ? `${base}, ${unmatched.length} not found` : base
  },
  table(data) {
    return {
      columns: ['From', 'To', 'From lon', 'From lat', 'To lon', 'To lat', 'Value'],
      rows: data.map((r) => [r.from ?? null, r.to ?? null, r.fromLon, r.fromLat, r.toLon, r.toLat, r.value]),
    }
  },
})

export default flowMap
