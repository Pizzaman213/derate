// What the map charts (choropleth, bubble-map, dot-map, flow-map, hex-tile-map) share:
// a projection fitted to the box, the base map, region-key resolution with a note for
// keys that match nothing, and value -> colour scales with their gradient legend.

import { z } from 'zod'
import { geoAlbersUsa, geoArea, geoCentroid, geoGraticule10, geoNaturalEarth1, geoPath, type GeoPath, type GeoPermissibleObjects, type GeoProjection } from 'd3-geo'
import type { Feature, MultiPolygon, Polygon } from 'geojson'
import { mapFeatures, resolveRegion, isCodeKey, type MapName, type Region, type RegionFeature } from '../../geo'
import type { Formatters, LegendSpec, Mark, Palette, Paint, Rect } from '../../core'

export const mapOption = z.enum(['world', 'us-states']).default('world').describe("base map: 'world' (countries, Natural Earth projection) or 'us-states' (Albers USA)")

export interface MapFrame {
  map: MapName
  projection: GeoProjection
  path: GeoPath
  /** Box the map was fitted into. */
  box: Rect
  /** Project [lon, lat]; null when the point is off this projection (Albers USA outside the US). */
  project(lon: number, lat: number): [number, number] | null
}

/** A projection fitted inside the box (minus `pad` on every side). */
export function fitMap(map: MapName, width: number, height: number, pad = 2): MapFrame {
  const box = { x: pad, y: pad, width: Math.max(1, width - 2 * pad), height: Math.max(1, height - 2 * pad) }
  const extent: [[number, number], [number, number]] = [[box.x, box.y], [box.x + box.width, box.y + box.height]]
  const projection = map === 'us-states'
    ? (geoAlbersUsa() as unknown as GeoProjection).fitExtent(extent, { type: 'FeatureCollection', features: mapFeatures(map) })
    : geoNaturalEarth1().fitExtent(extent, { type: 'Sphere' })
  const path = geoPath(projection).digits(1)
  return {
    map, projection, path, box,
    project(lon, lat) {
      const p = projection([lon, lat])
      return p && Number.isFinite(p[0]) && Number.isFinite(p[1]) ? [p[0], p[1]] : null
    },
  }
}

/** The projected path of a feature, or null when nothing of it is on the projection. */
export function featurePath(frame: MapFrame, f: GeoPermissibleObjects): string | null {
  const d = frame.path(f)
  return d && d.length > 1 ? d : null
}

/** Box of a feature on screen, for focus rings. */
export function featureBox(frame: MapFrame, f: GeoPermissibleObjects): Rect {
  const [[x0, y0], [x1, y1]] = frame.path.bounds(f)
  return Number.isFinite(x0) ? { x: x0, y: y0, width: Math.max(1, x1 - x0), height: Math.max(1, y1 - y0) } : { x: 0, y: 0, width: 1, height: 1 }
}

/**
 * Where a region's symbol goes, in lon/lat: the centroid of its largest polygon, so the
 * United States lands in Kansas rather than off Alaska and France in France rather than
 * the Atlantic. Memoised per feature.
 */
const lonLatMemo = new WeakMap<RegionFeature, [number, number]>()
export function regionLonLat(f: RegionFeature): [number, number] {
  const hit = lonLatMemo.get(f)
  if (hit) return hit
  const g = f.geometry
  let target: Feature<Polygon | MultiPolygon> | RegionFeature = f
  if (g.type === 'MultiPolygon') {
    let best = -1
    for (const coords of g.coordinates) {
      const poly: Feature<Polygon> = { type: 'Feature', properties: {}, geometry: { type: 'Polygon', coordinates: coords } }
      const a = geoArea(poly)
      if (a > best) { best = a; target = poly }
    }
  }
  const [lon, lat] = geoCentroid(target)
  const ll: [number, number] = [lon, lat]
  lonLatMemo.set(f, ll)
  return ll
}

/** The region's anchor on screen (see regionLonLat); the planar centroid if that is off the projection. */
export function regionAnchor(frame: MapFrame, f: RegionFeature): [number, number] | null {
  const p = frame.project(...regionLonLat(f))
  if (p) return p
  const c = frame.path.centroid(f)
  return Number.isFinite(c[0]) && Number.isFinite(c[1]) ? [c[0], c[1]] : null
}

/**
 * The base map: sphere and graticule on the world map, then every region in the track
 * colour. Regions listed in `skip` are left out (the chart draws them as data marks).
 */
export function baseMap(frame: MapFrame, theme: Palette, skip: ReadonlySet<string> = new Set()): Mark[] {
  const marks: Mark[] = []
  if (frame.map === 'world') {
    const sphere = frame.path({ type: 'Sphere' })
    if (sphere) marks.push({ type: 'path', layer: 'back', key: 'geo:sphere', d: sphere, fill: theme.surfaceRaised, stroke: theme.grid, strokeWidth: 1, inert: true })
    const grat = frame.path(geoGraticule10())
    if (grat) marks.push({ type: 'path', layer: 'back', key: 'geo:graticule', d: grat, fill: 'none', stroke: theme.grid, strokeWidth: 0.5, strokeOpacity: 0.6, inert: true })
  }
  for (const f of mapFeatures(frame.map)) {
    if (skip.has(f.properties.key)) continue
    const d = featurePath(frame, f)
    if (!d) continue
    marks.push({ type: 'path', layer: 'back', key: `geo:${f.properties.key}`, d, fill: theme.track, stroke: theme.surface, strokeWidth: 0.5, inert: true })
  }
  return marks
}

/** "Atlantis, Narnia and 3 more" for notes. */
export function nameList(names: string[], max = 4): string {
  const shown = names.slice(0, max).map((n) => (n.length > 40 ? `${n.slice(0, 39)}…` : n))
  return names.length > max ? `${shown.join(', ')} and ${names.length - max} more` : shown.join(', ')
}

/** "2 regions not found: Atlantis, Narnia" or undefined. */
export function unmatchedNote(names: string[], noun = 'region'): string | undefined {
  if (!names.length) return undefined
  const uniqNames = [...new Set(names)]
  return `${uniqNames.length} ${uniqNames.length === 1 ? noun : `${noun}s`} not found: ${nameList(uniqNames)}`
}

/** "40.71° N, 74.01° W" */
export function lonLatText(lon: number, lat: number): string {
  return `${Math.abs(lat).toFixed(2)}° ${lat >= 0 ? 'N' : 'S'}, ${Math.abs(lon).toFixed(2)}° ${lon >= 0 ? 'E' : 'W'}`
}

/** A finite lon/lat pair in range, or null. */
export function validLonLat(lon: number | null | undefined, lat: number | null | undefined): [number, number] | null {
  return typeof lon === 'number' && typeof lat === 'number' && Math.abs(lon) <= 180 && Math.abs(lat) <= 90 ? [lon, lat] : null
}

export interface Resolved {
  region: Region
  /** What to call it: the key as the user wrote it when it was a name, else the canonical name. */
  display: string
}

/** Resolve a key on a map; display keeps the user's own spelling of names (endonyms, RTL). */
export function resolveKey(map: MapName, key: string): Resolved | null {
  const region = resolveRegion(map, key)
  if (!region) return null
  return { region, display: isCodeKey(key) ? region.name : key.trim() }
}

export type ColorScale = 'linear' | 'log' | 'quantile'
export const colorScaleOption = z.enum(['linear', 'log', 'quantile']).default('linear').describe("value -> colour: 'linear', 'log' (positive values; tames outliers) or 'quantile' (equal-count classes)")

export interface RegionValue {
  region: Region
  display: string
  value: number | null
}

/**
 * Rows of { key, value } -> one item per matched region, first-seen order. Rows that name
 * the same region (any spelling) are summed; keys that match nothing are returned apart.
 */
export function collectRegions(map: MapName, rows: Array<{ key: string; value: number | null }>): { items: RegionValue[]; unmatched: string[]; duplicates: number; nulls: number } {
  const byKey = new Map<string, RegionValue>()
  const unmatched: string[] = []
  let duplicates = 0
  for (const r of rows) {
    const res = resolveKey(map, r.key)
    if (!res) { unmatched.push(r.key); continue }
    const hit = byKey.get(res.region.key)
    if (!hit) { byKey.set(res.region.key, { region: res.region, display: res.display, value: r.value }); continue }
    duplicates++
    hit.value = hit.value === null ? r.value : r.value === null ? hit.value : hit.value + r.value
  }
  const items = [...byKey.values()]
  return { items, unmatched, duplicates, nulls: items.filter((d) => d.value === null).length }
}

export interface ValueColors {
  color(v: number): Paint
  /** How far the colour sits from the light end of the ramp, 0..1: pick label ink by it. */
  intensity(v: number): number
  legend: LegendSpec
  /** Set when the requested scale could not be used. */
  note?: string
}

/**
 * Sequential colour over the values; diverging around zero when they span it.
 * `log` needs positive values (falls back to linear, noted); `quantile` uses 5 classes.
 */
export function valueColors(values: number[], theme: Palette, fmt: Formatters, scale: ColorScale, title?: string): ValueColors {
  const finite = values.filter((v) => Number.isFinite(v))
  const lo = finite.length ? Math.min(...finite) : 0
  const hi = finite.length ? Math.max(...finite) : 1
  if (lo < 0 && hi > 0) {
    const m = Math.max(-lo, hi)
    return {
      color: (v) => theme.div(0.5 + v / (2 * m)),
      intensity: (v) => Math.min(1, Math.abs(v) / m),
      legend: { title, gradient: { stops: theme.divStops(), min: fmt.value(-m), max: fmt.value(m), mid: fmt.value(0) } },
      note: scale !== 'linear' ? `${scale} colour scale needs values of one sign; linear used` : undefined,
    }
  }
  // the ramp starts a step in, so the lightest value still reads against the no-data track
  const ramp = (t: number) => theme.seq(0.15 + 0.85 * Math.min(1, Math.max(0, t)))
  const legend = (mid?: string): LegendSpec => ({ title, gradient: { stops: Array.from({ length: 9 }, (_, i) => ramp(i / 8)), min: fmt.value(lo), max: fmt.value(hi), mid } })
  const span = hi - lo
  if (scale === 'quantile' && finite.length > 1) {
    const sorted = [...finite].sort((a, b) => a - b)
    const k = 5
    const cuts = Array.from({ length: k - 1 }, (_, i) => sorted[Math.min(sorted.length - 1, Math.floor(((i + 1) * sorted.length) / k))]!)
    const cls = (v: number) => { let c = 0; while (c < cuts.length && v >= cuts[c]!) c++; return c }
    return {
      color: (v) => ramp(cls(v) / (k - 1)),
      intensity: (v) => cls(v) / (k - 1),
      legend: legend(fmt.value(sorted[Math.floor(sorted.length / 2)]!)),
    }
  }
  if (scale === 'log' && lo > 0 && hi > lo) {
    const a = Math.log(lo), b = Math.log(hi)
    const t = (v: number) => (Math.log(Math.max(lo, v)) - a) / (b - a)
    return { color: (v) => ramp(t(v)), intensity: t, legend: legend(fmt.value(Math.exp((a + b) / 2))) }
  }
  return {
    color: (v) => ramp(span > 0 ? (v - lo) / span : 0.6),
    intensity: (v) => (span > 0 ? (v - lo) / span : 0.6),
    legend: legend(),
    note: scale === 'log' && span > 0 ? 'log colour scale needs positive values; linear used' : undefined,
  }
}
