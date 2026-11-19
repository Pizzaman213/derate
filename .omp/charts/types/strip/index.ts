// Strip (jitter) plot: one point per row in its group's band, spread across the band by a
// seeded jitter (ctx.random) so the same data and seed always draw the same picture.

import { defineChart, phrase, stats, type Mark, type ScaleSpec } from '../../core'
import { finiteExtent, notes, uniq } from '../../core/helpers'
import { autoRadius, finiteValues, padExtent, spread } from '../_shared/DistributionB'
import { input, options, type StripOptions, type StripRow } from './schema'

export const strip = defineChart<StripRow[], StripOptions>({
  id: 'strip',
  name: 'Strip plot',
  category: 'distribution',
  intents: ['distribution'],
  summary: 'Every value as a jittered point in its group, with the group median marked.',
  input,
  options,
  shape: 'rows',
  fields: ['group', 'value'],
  numberFields: ['value'],
  isEmpty(data) {
    if (data.length === 0) return true
    return finiteValues(data).ok.length === 0 ? 'No values to plot' : false
  },
  draw(ctx) {
    const { theme, options: o, fmt } = ctx
    const { ok, nulls } = finiteValues(ctx.data)
    const groups = uniq(ok.map((d) => d.row.group))
    const ext = padExtent(finiteExtent(ok.map((d) => d.value)) ?? [0, 1], 0.03)
    const vert = o.orientation === 'vertical'
    const valueSpec: ScaleSpec = { type: 'linear', domain: ext, zero: false }
    const bandSpec: ScaleSpec = { type: 'band', domain: groups, padding: 0.2 }
    const f = vert
      ? ctx.cartesian({ x: bandSpec, y: valueSpec, xTitle: o.groupTitle, yTitle: o.valueTitle, grid: 'y' })
      : ctx.cartesian({ x: valueSpec, y: bandSpec, xTitle: o.valueTitle, yTitle: o.groupTitle, grid: 'x' })
    const breadth = vert ? f.bw : f.bh
    const length = vert ? f.plot.height : f.plot.width
    const byGroup = groups.map((g) => ok.filter((d) => d.row.group === g))
    const spreadPx = breadth * o.jitter
    const r = o.radius ?? autoRadius(Math.max(...byGroup.map((g) => g.length)), length * Math.max(spreadPx, 4), 1.25, 5, 0.6)
    const reach = Math.max(0, spreadPx - 2 * r)

    const marks: Mark[] = [...f.marks]
    byGroup.forEach((items, gi) => {
      const g = groups[gi]!
      const centre = (vert ? f.px(g) : f.py(g)) + breadth / 2
      const fill = theme.cat(gi)
      const offsets = stats.jitter(items.length, reach, ctx.random)
      items.forEach((d, i) => {
        const along = vert ? f.py(d.value) : f.px(d.value)
        const across = centre + offsets[i]!
        marks.push({
          type: 'circle', role: 'point', key: `p:${d.index}`,
          cx: vert ? across : along, cy: vert ? along : across, r,
          fill, fillOpacity: 0.7,
          datum: d.row, label: `${g}: ${fmt.value(d.value)}`, tooltip: { title: g, rows: [['Value', fmt.value(d.value)]], color: fill },
        })
      })
      if (o.showMedian && items.length > 1) {
        const m = stats.median(items.map((d) => d.value))
        const at = vert ? f.py(m) : f.px(m)
        const half = Math.min(breadth / 2, spreadPx / 2 + r + 3)
        marks.push(vert
          ? { type: 'line', layer: 'labels', key: `med:${g}`, x1: centre - half, x2: centre + half, y1: at, y2: at, stroke: theme.ink, strokeWidth: 2, linecap: 'round', inert: true }
          : { type: 'line', layer: 'labels', key: `med:${g}`, y1: centre - half, y2: centre + half, x1: at, x2: at, stroke: theme.ink, strokeWidth: 2, linecap: 'round', inert: true })
      }
    })
    return {
      marks,
      plot: f.plot,
      note: notes([[nulls, 'missing values skipped']]),
    }
  },
  describe({ data, fmt, name }) {
    const { ok } = finiteValues(data)
    const groups = uniq(ok.map((d) => d.row.group))
    let s = `${name}, ${phrase.count(ok.length, 'value')} in ${phrase.count(groups.length, 'group')}, ${spread(ok.map((d) => d.value), fmt)}`
    if (groups.length > 1) {
      const medians = groups.map((g) => ({ label: g, value: stats.median(ok.filter((d) => d.row.group === g).map((d) => d.value)) }))
      if (medians.some((m) => m.value !== medians[0]!.value)) s += `; group medians ${phrase.extremes(medians, fmt)}`
    }
    return s
  },
  table(data) {
    return { columns: ['Group', 'Value'], rows: data.map((r) => [r.group, r.value]) }
  },
})

export default strip
