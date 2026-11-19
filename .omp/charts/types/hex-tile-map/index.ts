// Hex-tile map: every US state (and DC) as one equal hexagon on a standard grid, so
// Rhode Island weighs as much as Texas. Colour carries the value; the postal code labels it.

import { defineChart, phrase, SIZES } from '../../core'
import { notes } from '../../core/helpers'
import type { Mark } from '../../core'
import { US_HEX, US_STATES } from '../../geo'
import { collectRegions, nameList, resolveKey, unmatchedNote, valueColors } from '../_shared/GeoCharts'
import { input, options, type HexTileOptions, type HexTileRow } from './schema'

const SQRT3 = Math.sqrt(3)
/** Inset of each tile, as a share of its radius: the gutter between tiles. */
const INSET = 0.93

export interface HexCell { postal: string; cx: number; cy: number; r: number }

/** Pointy-top hexes, odd rows offset half a tile right, centred in width x height. */
export function hexLayout(width: number, height: number, pad = 2): HexCell[] {
  const cells = Object.entries(US_HEX)
  const maxX = Math.max(...cells.map(([, c]) => c.col + (c.row % 2) * 0.5))
  const maxRow = Math.max(...cells.map(([, c]) => c.row))
  // extent in units of r: width (maxX + 1) * sqrt3, height 2 + maxRow * 1.5
  const uw = (maxX + 1) * SQRT3, uh = 2 + maxRow * 1.5
  const r = Math.max(1, Math.min((width - 2 * pad) / uw, (height - 2 * pad) / uh))
  const x0 = (width - uw * r) / 2 + (SQRT3 * r) / 2
  const y0 = (height - uh * r) / 2 + r
  return cells.map(([postal, c]) => ({ postal, cx: x0 + (c.col + (c.row % 2) * 0.5) * SQRT3 * r, cy: y0 + c.row * 1.5 * r, r }))
}

const hexPath = (cx: number, cy: number, r: number) =>
  Array.from({ length: 6 }, (_, i) => {
    const a = (Math.PI / 3) * i - Math.PI / 2
    return `${i ? 'L' : 'M'}${(cx + r * Math.cos(a)).toFixed(1)},${(cy + r * Math.sin(a)).toFixed(1)}`
  }).join('') + 'Z'

const collect = (data: HexTileRow[]) => {
  const c = collectRegions('us-states', data.map((r) => ({ key: r.state, value: r.value })))
  const offGrid = c.items.filter((d) => !US_HEX[d.region.key])
  return { ...c, items: c.items.filter((d) => US_HEX[d.region.key]), offGrid: offGrid.map((d) => d.display) }
}

export const hexTileMap = defineChart<HexTileRow[], HexTileOptions>({
  id: 'hex-tile-map',
  name: 'Hex-tile map',
  category: 'geo',
  intents: ['geo'],
  summary: 'US states as equal hexagons on a grid, coloured by a value.',
  input,
  options,
  shape: 'rows',
  fields: ['state', 'value'],
  numberFields: ['value'],
  aspect: 0.6,
  minHeight: 160,
  draw(ctx) {
    const { theme, options: o, fmt } = ctx
    const { items, unmatched, duplicates, offGrid } = collect(ctx.data)
    const byKey = new Map(items.map((d) => [d.region.key, d]))
    const valued = items.filter((d) => d.value !== null).map((d) => d.value as number)
    const scale = valueColors(valued, theme, fmt, o.scale, o.valueLabel)
    const cells = hexLayout(ctx.width, ctx.height)
    const r = cells[0]?.r ?? 1
    const tileW = SQRT3 * r * INSET
    const size = Math.min(SIZES.label, Math.max(SIZES.tick, r * 0.55))
    const showCodes = ctx.measure('WW', size, 600) <= tileW - 4 && r * 0.9 >= size
    const showVals = o.showValues && showCodes && r >= size * 2.2
    const name = new Map(US_STATES.map((s) => [s.postal, s.name]))
    const marks: Mark[] = []
    const labels: Mark[] = []
    // data order first so keyboard order follows the data; then the tiles with no row
    const order = [...items.map((d) => d.region.key), ...cells.map((c) => c.postal).filter((p) => !byKey.has(p))]
    const cellOf = new Map(cells.map((c) => [c.postal, c]))
    let nulls = 0
    for (const postal of order) {
      const c = cellOf.get(postal)!
      const d = byKey.get(postal)
      const tile = { type: 'path' as const, key: `hex:${postal}`, d: hexPath(c.cx, c.cy, r * INSET), cx: c.cx, cy: c.cy, box: { x: c.cx - tileW / 2, y: c.cy - r * INSET, width: tileW, height: 2 * r * INSET } }
      let ink = theme.muted
      if (d && d.value !== null) {
        const fill = scale.color(d.value)
        const v = fmt.value(d.value)
        marks.push({
          ...tile, role: 'hex', fill, stroke: 'none',
          datum: { state: d.display, postal, name: d.region.name, value: d.value },
          label: `${d.region.name}: ${v}`,
          tooltip: { title: d.region.name, rows: [[o.valueLabel ?? 'Value', v]], color: fill },
        })
        ink = 0.15 + 0.85 * scale.intensity(d.value) > 0.55 ? theme.surface : theme.ink
      } else {
        if (d) nulls++
        marks.push({ ...tile, role: 'hex-empty', fill: theme.track, stroke: 'none', label: `${name.get(postal) ?? postal}: no data`, inert: true })
      }
      if (!showCodes) continue
      const withVal = showVals && d && d.value !== null
      labels.push({ type: 'text', layer: 'labels', key: `code:${postal}`, x: c.cx, y: withVal ? c.cy - size * 0.15 : c.cy, text: postal, anchor: 'middle', baseline: withVal ? 'alphabetic' : 'central', size, weight: 600, fill: ink, inert: true })
      if (withVal) {
        const vs = ctx.truncate(fmt.value(d.value), tileW - 4, SIZES.tick)
        if (vs) labels.push({ type: 'text', layer: 'labels', key: `val:${postal}`, x: c.cx, y: c.cy + SIZES.tick * 0.2, text: vs, anchor: 'middle', baseline: 'hanging', size: SIZES.tick, fill: ink, inert: true })
      }
    }
    return {
      marks: [...marks, ...labels],
      legend: scale.legend,
      note: [
        unmatchedNote(unmatched, 'state'),
        offGrid.length ? `${offGrid.length} not on the hex grid: ${nameList(offGrid)}` : undefined,
        notes([[duplicates, 'duplicate states summed'], [nulls, nulls === 1 ? 'state without a value left grey' : 'states without a value left grey']]),
        scale.note,
      ].filter(Boolean).join('; ') || undefined,
    }
  },
  describe({ data, options: o, fmt, name }) {
    const { items, unmatched } = collect(data)
    const valued = items.filter((d) => d.value !== null)
    const base = `${name}, ${phrase.count(valued.length, 'state')}${o.valueLabel ? ` by ${o.valueLabel}` : ''}, ${phrase.extremes(items.map((d) => ({ label: d.region.name, value: d.value })), fmt)}`
    return unmatched.length ? `${base}, ${unmatched.length} not found` : base
  },
  table(data) {
    return {
      columns: ['State', 'Matched', 'Value'],
      rows: data.map((r) => [r.state, resolveKey('us-states', r.state)?.region.name ?? 'not found', r.value]),
    }
  },
})

export default hexTileMap
