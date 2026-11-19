// Choropleth: each region of a base map (world countries or US states) filled by its value.

import { defineChart, phrase } from '../../core'
import { notes } from '../../core/helpers'
import type { Mark } from '../../core'
import { baseMap, collectRegions, featureBox, featurePath, fitMap, nameList, regionAnchor, resolveKey, unmatchedNote, valueColors } from '../_shared/GeoCharts'
import { input, options, type ChoroplethOptions, type ChoroplethRow } from './schema'

const noun = (map: ChoroplethOptions['map']) => (map === 'us-states' ? 'state' : 'country')
const plural = (map: ChoroplethOptions['map']) => (map === 'us-states' ? 'states' : 'countries')
const collect = (data: ChoroplethRow[], o: ChoroplethOptions) => collectRegions(o.map, data.map((r) => ({ key: r.region, value: r.value })))

export const choropleth = defineChart<ChoroplethRow[], ChoroplethOptions>({
  id: 'choropleth',
  name: 'Choropleth map',
  category: 'geo',
  intents: ['geo', 'compare'],
  summary: 'Countries or US states shaded by a value.',
  input,
  options,
  shape: 'rows',
  fields: ['region', 'value'],
  numberFields: ['value'],
  aspect: 0.56,
  minHeight: 170,
  draw(ctx) {
    const { theme, options: o, fmt } = ctx
    const { items, unmatched, duplicates, nulls } = collect(ctx.data, o)
    const frame = fitMap(o.map, ctx.width, ctx.height)
    const valued = items.filter((d): d is typeof d & { value: number } => d.value !== null)
    const scale = valueColors(valued.map((d) => d.value), theme, fmt, o.scale, o.valueLabel)
    const marks: Mark[] = baseMap(frame, theme, new Set(valued.map((d) => d.region.key)))
    const offMap: string[] = []
    for (const d of valued) {
      const f = d.region.feature
      const path = featurePath(frame, f)
      if (!path) { offMap.push(d.display); continue }
      const at = regionAnchor(frame, f) ?? [frame.box.x + frame.box.width / 2, frame.box.y + frame.box.height / 2]
      const fill = scale.color(d.value)
      const v = fmt.value(d.value)
      marks.push({
        type: 'path', role: 'region', key: `region:${d.region.key}`, d: path, fill, stroke: theme.surface, strokeWidth: 0.5,
        cx: at[0], cy: at[1], box: featureBox(frame, f),
        datum: { region: d.display, key: d.region.key, name: d.region.name, value: d.value },
        label: `${d.display}: ${v}`,
        tooltip: { title: d.display, rows: [[o.valueLabel ?? 'Value', v]], color: fill },
      })
    }
    return {
      marks,
      legend: scale.legend,
      note: [
        unmatchedNote(unmatched),
        offMap.length ? `${offMap.length} not drawn on this projection: ${nameList(offMap)}` : undefined,
        notes([[duplicates, 'duplicate regions summed'], [nulls, `${nulls === 1 ? noun(o.map) : plural(o.map)} without a value left grey`]]),
        scale.note,
      ].filter(Boolean).join('; ') || undefined,
    }
  },
  describe({ data, options: o, fmt, name }) {
    const { items, unmatched } = collect(data, o)
    const valued = items.filter((d) => d.value !== null)
    const base = `${name}, ${phrase.count(valued.length, noun(o.map), plural(o.map))}${o.valueLabel ? ` by ${o.valueLabel}` : ''}, ${phrase.extremes(items.map((d) => ({ label: d.display, value: d.value })), fmt)}`
    return unmatched.length ? `${base}, ${unmatched.length} not found` : base
  },
  table(data, o) {
    return {
      columns: ['Region', 'Matched', 'Value'],
      rows: data.map((r) => [r.region, resolveKey(o.map, r.region)?.region.name ?? 'not found', r.value]),
    }
  },
})

export default choropleth
