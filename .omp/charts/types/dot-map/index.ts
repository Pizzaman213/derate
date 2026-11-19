// Dot map: one dot per row at its lon/lat on a base map, coloured by category.
// Above 5,000 dots core switches to canvas; the legend toggles categories.

import { defineChart, phrase } from '../../core'
import { notes } from '../../core/helpers'
import type { LegendSpec, Mark } from '../../core'
import { baseMap, fitMap, lonLatText, validLonLat } from '../_shared/GeoCharts'
import { input, options, type DotMapOptions, type DotMapRow } from './schema'

export const OTHER = 'Other'

/** Categories by frequency, the tail beyond `max` merged into "Other". */
export function categoriesOf(data: DotMapRow[], max: number): { names: string[]; of: (c: string | undefined) => string | undefined; merged: number } {
  const counts = new Map<string, number>()
  for (const r of data) if (r.category !== undefined) counts.set(r.category, (counts.get(r.category) ?? 0) + 1)
  // keep first-seen order among the kept categories so colours are stable as data grows
  const ranked = [...counts.entries()].sort((a, b) => b[1] - a[1])
  if (ranked.length <= max) return { names: [...counts.keys()], of: (c) => c, merged: 0 }
  const keep = new Set(ranked.slice(0, max - 1).map(([k]) => k))
  return {
    names: [...[...counts.keys()].filter((k) => keep.has(k)), OTHER],
    of: (c) => (c === undefined ? undefined : keep.has(c) ? c : OTHER),
    merged: ranked.length - (max - 1),
  }
}

export const dotMap = defineChart<DotMapRow[], DotMapOptions>({
  id: 'dot-map',
  name: 'Dot map',
  category: 'geo',
  intents: ['geo', 'distribution'],
  summary: 'One dot per location on a world or US map, coloured by category.',
  input,
  options,
  shape: 'rows',
  fields: ['lon', 'lat', 'category'],
  numberFields: ['lon', 'lat'],
  aspect: 0.56,
  minHeight: 170,
  draw(ctx) {
    const { theme, options: o } = ctx
    const frame = fitMap(o.map, ctx.width, ctx.height)
    const cats = categoriesOf(ctx.data, o.maxCategories)
    const color = new Map(cats.names.map((c, i) => [c, theme.cat(i)]))
    const n = ctx.data.length
    const r = o.radius ?? (n <= 300 ? 3.5 : n <= 1500 ? 2.5 : n <= 5000 ? 1.75 : 1.25)
    const marks: Mark[] = baseMap(frame, theme)
    let invalid = 0, offMap = 0
    ctx.data.forEach((row, i) => {
      const ll = validLonLat(row.lon, row.lat)
      if (!ll) { invalid++; return }
      const p = frame.project(ll[0], ll[1])
      if (!p) { offMap++; return }
      const series = cats.of(row.category)
      const fill = series !== undefined ? color.get(series)! : theme.cat(0)
      const where = lonLatText(ll[0], ll[1])
      marks.push({
        type: 'circle', role: 'dot', key: `d:${i}`, cx: p[0], cy: p[1], r, fill, fillOpacity: 0.8,
        stroke: n <= 1500 ? theme.surface : 'none', strokeWidth: 0.5, series,
        datum: row,
        label: row.category !== undefined ? `${row.category}: ${where}` : where,
        tooltip: { title: row.category ?? 'Location', rows: [['Location', where]], color: fill },
      })
    })
    const legend: LegendSpec | undefined = cats.names.length > 1 ? { items: cats.names.map((c) => ({ key: c, label: c, color: color.get(c)!, shape: 'circle' })) } : undefined
    return {
      marks,
      legend,
      note: notes([[invalid, `${invalid === 1 ? 'row' : 'rows'} without a valid lon/lat skipped`], [offMap, `outside the ${o.map === 'us-states' ? 'US' : 'world'} map`], [cats.merged, 'smaller categories merged into Other']]),
    }
  },
  describe({ data, options: o, name }) {
    const valid = data.filter((r) => validLonLat(r.lon, r.lat))
    const cats = categoriesOf(valid, o.maxCategories)
    const lons = valid.map((r) => r.lon as number), lats = valid.map((r) => r.lat as number)
    const span = valid.length ? `, from ${Math.min(...lats).toFixed(1)}° to ${Math.max(...lats).toFixed(1)}° latitude and ${Math.min(...lons).toFixed(1)}° to ${Math.max(...lons).toFixed(1)}° longitude` : ''
    return `${name}, ${phrase.count(valid.length, 'location')}${cats.names.length > 1 ? ` in ${phrase.count(cats.names.length, 'category', 'categories')} (${phrase.listPhrase(cats.names)})` : ''}${span}`
  },
  table(data) {
    return { columns: ['Longitude', 'Latitude', 'Category'], rows: data.map((r) => [r.lon, r.lat, r.category ?? null]) }
  },
})

export default dotMap
