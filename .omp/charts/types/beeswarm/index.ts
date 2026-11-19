// Beeswarm: one circle per row along a value axis, dodged sideways (stats.beeswarm) so no
// two overlap; optional groups get a swarm each.

import { defineChart, phrase, stats, type Mark, type ScaleSpec } from '../../core'
import { finiteExtent, notes, uniq } from '../../core/helpers'
import { autoRadius, finiteValues, padExtent, spread, uniqueKeys, type Indexed } from '../_shared/DistributionB'
import { input, options, type BeeswarmOptions, type BeeswarmRow } from './schema'

const BLANK = '(blank)'
const groupOf = (r: BeeswarmRow, grouped: boolean) => (grouped ? r.group ?? BLANK : '')
const isGrouped = (rows: BeeswarmRow[]) => rows.some((r) => r.group !== undefined)
const usableOf = (rows: BeeswarmRow[], o: BeeswarmOptions) => {
  const { ok, nulls } = finiteValues(rows)
  const usable = o.scale === 'log' ? ok.filter((d) => d.value > 0) : ok
  return { usable, nulls, nonPositive: ok.length - usable.length }
}

interface Placed { d: Indexed<BeeswarmRow>; pos: number; offset: number }

export const beeswarm = defineChart<BeeswarmRow[], BeeswarmOptions>({
  id: 'beeswarm',
  name: 'Beeswarm chart',
  category: 'distribution',
  intents: ['distribution'],
  summary: 'Every value as its own circle along an axis, dodged so none overlap.',
  input,
  options,
  shape: 'rows',
  fields: ['value', 'group', 'label'],
  numberFields: ['value'],
  isEmpty(data, o) {
    if (data.length === 0) return true
    return usableOf(data, o).usable.length === 0 ? (o.scale === 'log' ? 'No positive values to plot on a log scale' : 'No values to plot') : false
  },
  draw(ctx) {
    const { theme, options: o, fmt } = ctx
    const grouped = isGrouped(ctx.data)
    const { usable, nulls, nonPositive } = usableOf(ctx.data, o)
    const groups = grouped ? uniq(usable.map((d) => groupOf(d.row, true))) : ['']
    const ext0 = finiteExtent(usable.map((d) => d.value)) ?? [0, 1]
    const ext = o.scale === 'log' ? ext0 : padExtent(ext0, 0.02)
    const horiz = o.orientation === 'horizontal'
    const valueSpec: ScaleSpec = o.scale === 'log' ? { type: 'log', domain: ext } : { type: 'linear', domain: ext, zero: false }
    const bandSpec: ScaleSpec = { type: 'band', domain: groups, padding: grouped ? 0.12 : 0 }
    const f = horiz
      ? ctx.cartesian({ x: valueSpec, y: bandSpec, xTitle: o.valueTitle, yTitle: grouped ? o.groupTitle : undefined, yAxis: grouped ? 'left' : 'none', grid: 'x' })
      : ctx.cartesian({ x: bandSpec, y: valueSpec, xTitle: grouped ? o.groupTitle : undefined, yTitle: o.valueTitle, xAxis: grouped ? 'bottom' : 'none', grid: 'y' })
    const breadth = horiz ? f.bh : f.bw
    const length = horiz ? f.plot.width : f.plot.height
    const valuePx = (v: number) => (horiz ? f.px(v) : f.py(v))
    const byGroup = groups.map((g) => usable.filter((d) => groupOf(d.row, grouped) === g).sort((a, b) => a.value - b.value))

    // stats.beeswarm returns its circles in ascending value order, matching the sorted rows
    const swarm = (items: Array<Indexed<BeeswarmRow>>, r: number): Placed[] =>
      stats.beeswarm(items.map((d) => d.value), { radius: r, scale: valuePx }).map((p, i) => ({ d: items[i]!, pos: p.x, offset: p.offset }))
    const reachOf = (layout: Placed[][], r: number) => Math.max(0, ...layout.map((g) => Math.max(0, ...g.map((p) => Math.abs(p.offset))))) + r

    // shrink the circles until every swarm fits its band (unless the radius is fixed)
    const maxN = Math.max(...byGroup.map((g) => g.length))
    let r = o.radius ?? autoRadius(maxN, length * breadth, 1, 6, 0.3)
    let layout = byGroup.map((g) => swarm(g, r))
    for (let pass = 0; pass < 8 && o.radius === undefined; pass++) {
      const reach = reachOf(layout, r)
      if (reach <= breadth / 2 || r <= 0.75) break
      r = Math.max(0.75, r * Math.min(0.92, Math.sqrt(breadth / 2 / reach)))
      layout = byGroup.map((g) => swarm(g, r))
    }

    const keys = uniqueKeys(ctx.data.map((d) => d.label), 'p:')
    const marks: Mark[] = [...f.marks]
    const limit = Math.max(0, breadth / 2 - r)
    let pressed = 0
    layout.forEach((placed, gi) => {
      const g = groups[gi]!
      const centre = (horiz ? f.py(g) : f.px(g)) + breadth / 2
      const fill = grouped ? theme.cat(gi) : theme.cat(0)
      for (const p of placed) {
        let off = p.offset
        if (Math.abs(off) > limit + 1e-6) { pressed++; off = Math.sign(off) * limit }
        const { row, value } = p.d
        const name = row.label !== undefined ? (grouped ? `${row.label} (${g})` : row.label) : grouped ? g : 'Value'
        const rows: Array<[string, string]> = [['Value', fmt.value(value)]]
        if (grouped && row.label !== undefined) rows.push(['Group', g])
        marks.push({
          type: 'circle', role: 'point', key: keys[p.d.index]!,
          cx: horiz ? p.pos : centre + off, cy: horiz ? centre - off : p.pos, r,
          fill, fillOpacity: 0.9, stroke: r >= 2.5 ? theme.surface : undefined, strokeWidth: r >= 2.5 ? 0.75 : undefined,
          datum: row, label: `${name}: ${fmt.value(value)}`, tooltip: { title: row.label ?? (grouped ? g : 'Value'), rows, color: fill },
        })
      }
    })
    return {
      marks,
      plot: f.plot,
      note: notes([[nulls, 'missing values skipped'], [nonPositive, 'values ≤ 0 skipped on the log scale'], [pressed, 'points pressed into a full band (they overlap)']]),
    }
  },
  describe({ data, options: o, fmt, name }) {
    const grouped = isGrouped(data)
    const { usable } = usableOf(data, o)
    const values = usable.map((d) => d.value)
    let s = `${name}, ${phrase.count(values.length, 'value')}`
    if (grouped) {
      const groups = uniq(usable.map((d) => groupOf(d.row, true)))
      s += ` in ${phrase.count(groups.length, 'group')}`
      s += `, ${spread(values, fmt)}`
      if (groups.length > 1) {
        const medians = groups.map((g) => ({ label: g, value: stats.median(usable.filter((d) => groupOf(d.row, true) === g).map((d) => d.value)) }))
        const top = medians.reduce((a, b) => (b.value > a.value ? b : a))
        if (medians.some((m) => m.value !== top.value)) s += `, highest median ${top.label} at ${fmt.value(top.value)}`
      }
      return s
    }
    return `${s}, ${spread(values, fmt)}`
  },
  table(data) {
    const grouped = isGrouped(data)
    const labelled = data.some((r) => r.label !== undefined)
    const columns = [...(labelled ? ['Label'] : []), ...(grouped ? ['Group'] : []), 'Value']
    return {
      columns,
      rows: data.map((r) => [...(labelled ? [r.label ?? null] : []), ...(grouped ? [r.group ?? BLANK] : []), r.value]),
    }
  },
})

export default beeswarm
