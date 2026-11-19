// UpSet plot: every non-empty exclusive intersection (items in exactly that combination of
// sets) as a bar, the combination spelled out underneath as a column of connected dots,
// and each set's total as a bar on the left. Exact where Venn and Euler diagrams cannot
// be: any number of sets, and sizes are read off a common baseline instead of areas.

import { scaleLinear } from 'd3-scale'
import { defineChart, phrase, SIZES } from '../../core'
import type { Formatters, Mark } from '../../core'
import { notes } from '../../core/helpers'
import { adaptAreas, areaKey } from '../_shared/SetCharts'
import { input, options, type UpsetOptions, type UpsetRow } from './schema'

export interface Combo {
  key: string
  /** Member sets, in set order (largest set first). */
  sets: string[]
  /** Items in exactly these sets and no other. */
  size: number
}

export interface Intersections {
  /** Sets, largest first, with their totals (sum of the combos they are in). */
  sets: Array<{ name: string; size: number }>
  /** Non-empty exclusive intersections, largest first. */
  combos: Combo[]
  /** Whether the rows were aggregated sizes (else item memberships). */
  aggregated: boolean
  counts: { repeated: number; none: number; nulls: number; negatives: number; duplicates: number; inconsistent: number }
}

/** Exclusive intersection sizes from memberships (counted) or from aggregated sizes. */
export function intersections(rows: readonly UpsetRow[], sizes: 'inclusive' | 'exclusive'): Intersections {
  const counts = { repeated: 0, none: 0, nulls: 0, negatives: 0, duplicates: 0, inconsistent: 0 }
  const seen = new Map<string, number>()
  const note = (name: string) => { if (!seen.has(name)) seen.set(name, seen.size) }
  const exclusive = new Map<string, { sets: string[]; size: number }>()
  const add = (sets: string[], n: number) => {
    const k = areaKey(sets)
    const hit = exclusive.get(k)
    if (hit) hit.size += n
    else exclusive.set(k, { sets, size: n })
  }
  const aggregated = rows.some((r) => r.size !== null)

  if (!aggregated) {
    // memberships: rows sharing an item are one item (long format)
    const named = new Map<string, Set<string>>()
    const anonymous: Array<Set<string>> = []
    for (const r of rows) {
      r.sets.forEach(note)
      if (r.item === undefined) { anonymous.push(new Set(r.sets)); continue }
      const hit = named.get(r.item)
      if (!hit) named.set(r.item, new Set(r.sets))
      else if (r.sets.every((s) => hit.has(s))) counts.repeated++ // adds nothing: a true duplicate
      else r.sets.forEach((s) => hit.add(s))
    }
    for (const s of [...named.values(), ...anonymous]) {
      if (s.size === 0) counts.none++
      else add([...s], 1)
    }
  } else {
    const listed = new Map<string, { sets: string[]; size: number }>()
    for (const r of rows) {
      if (r.size === null) { counts.nulls++; continue }
      if (r.size < 0) { counts.negatives++; continue }
      if (r.sets.length === 0) { counts.none += r.size; continue }
      r.sets.forEach(note)
      const k = areaKey(r.sets)
      const hit = listed.get(k)
      if (hit) { counts.duplicates++; hit.size += r.size } else listed.set(k, { sets: r.sets, size: r.size })
    }
    if (sizes === 'exclusive') for (const a of listed.values()) add(a.sets, a.size)
    else {
      // inclusive -> exclusive by inclusion-exclusion over the listed supersets
      for (const a of listed.values()) {
        let v = 0
        for (const b of listed.values()) {
          if (b.sets.length < a.sets.length || !a.sets.every((s) => b.sets.includes(s))) continue
          v += (b.sets.length - a.sets.length) % 2 === 0 ? b.size : -b.size
        }
        if (v < -1e-9) counts.inconsistent++
        add(a.sets, Math.max(0, v))
      }
    }
  }

  const total = new Map<string, number>([...seen.keys()].map((n) => [n, 0]))
  for (const c of exclusive.values()) for (const s of c.sets) total.set(s, total.get(s)! + c.size)
  const sets = [...total].map(([name, size]) => ({ name, size })).sort((a, b) => b.size - a.size || seen.get(a.name)! - seen.get(b.name)!)
  const order = new Map(sets.map((s, i) => [s.name, i]))
  const combos = [...exclusive.entries()]
    .filter(([, c]) => c.size > 0)
    .map(([key, c]) => ({ key, sets: [...c.sets].sort((a, b) => order.get(a)! - order.get(b)!), size: c.size }))
    .sort((a, b) => b.size - a.size || a.sets.length - b.sets.length || a.key.localeCompare(b.key))
  return { sets, combos, aggregated, counts }
}

const comboName = (c: Combo) => (c.sets.length === 1 ? `${c.sets[0]} only` : `${phrase.listPhrase(c.sets, 12)} only`)
const pct = (fmt: Formatters, v: number, of: number) => (of > 0 ? fmt.pct(v / of, 1) : '—')

export const upset = defineChart<UpsetRow[], UpsetOptions>({
  id: 'upset',
  name: 'UpSet plot',
  category: 'sets',
  intents: ['sets', 'compare', 'rank'],
  summary: 'Exact sizes of every set intersection as bars over a dot matrix, set totals beside.',
  input,
  options,
  shape: 'object',
  fields: ['item', 'sets', 'size'],
  numberFields: ['size'],
  aspect: 0.62,
  minHeight: 240,
  adapt: (raw, opts) => adaptAreas(raw, opts, '{ item?, sets } or { sets, size }'),
  isEmpty(data, opts) {
    if (!data.length) return true
    const m = intersections(data, opts.sizes)
    if (m.combos.length) return false
    return m.aggregated ? 'All sizes are zero' : 'No item belongs to any set'
  },
  draw(ctx) {
    const { theme, fmt, options: o, width: W, height: H } = ctx
    const m = intersections(ctx.data, o.sizes)
    const tick = SIZES.tick, labelSize = SIZES.label
    const captionH = tick + 8

    // rows: one per set, as many as fit in a little over half the height
    const minRow = tick + 6
    const maxRows = Math.max(1, Math.floor(((H - captionH) * 0.55) / minRow))
    const sets = m.sets.slice(0, maxRows)
    const droppedSets = m.sets.length - sets.length
    const rowOf = new Map(sets.map((s, i) => [s.name, i]))
    const reachable = m.combos.filter((c) => c.sets.every((s) => rowOf.has(s)))
    const rowH = Math.min(26, Math.max(minRow, ((H - captionH) * 0.42) / sets.length))
    const matrixH = sets.length * rowH
    const matrixTop = H - captionH - matrixH

    // columns: left to right are set bars, set names, then the matrix
    const longest = Math.max(...sets.map((s) => ctx.measure(s.name, labelSize)))
    const labelW = Math.min(W * 0.24, longest + 10)
    const setBarW = Math.max(36, Math.min(170, W * 0.17))
    const x0 = setBarW + labelW + 6
    const plotW = Math.max(10, W - x0 - 2)
    const minCol = Math.max(tick + 3, Math.min(rowH, 18))
    const maxCols = Math.max(1, Math.floor(plotW / minCol))
    const shown = reachable.slice(0, maxCols)
    const omitted = reachable.length - shown.length + (m.combos.length - reachable.length)
    if (o.sortBy === 'degree') shown.sort((a, b) => a.sets.length - b.sets.length || b.size - a.size || a.key.localeCompare(b.key))
    const colW = Math.min(plotW / Math.max(1, shown.length), Math.max(minCol, 64))

    // intersection bars over the matrix
    const top = (o.showValues ? tick + 6 : 4) + tick / 2
    const barsBottom = matrixTop - 8
    const maxCombo = Math.max(1, ...shown.map((c) => c.size))
    const tickCount = Math.max(2, Math.floor((barsBottom - top) / 34))
    const y = scaleLinear().domain([0, maxCombo]).range([barsBottom, top]).nice(tickCount)
    // counts of items are whole numbers: no 0.5-item gridlines
    const integral = shown.every((c) => Number.isInteger(c.size))
    const yTicks = y.ticks(tickCount).filter((t) => !integral || Number.isInteger(t))

    const marks: Mark[] = []
    const matrixRight = x0 + shown.length * colW
    for (const t of yTicks) {
      const py = y(t)
      marks.push({ type: 'line', layer: 'grid', key: `g:${t}`, x1: x0, x2: matrixRight, y1: py, y2: py, stroke: theme.grid, strokeWidth: 1, inert: true })
      marks.push({ type: 'text', layer: 'axes', key: `yt:${t}`, x: x0 - 6, y: py, text: fmt.value(t), anchor: 'end', baseline: 'central', size: tick, fill: theme.muted })
    }
    marks.push({ type: 'line', layer: 'axes', key: 'y-axis', x1: x0, x2: x0, y1: top, y2: barsBottom, stroke: theme.axis, strokeWidth: 1, inert: true })
    const tickW = Math.max(0, ...yTicks.map((t) => ctx.measure(fmt.value(t), tick)))
    const titleX = x0 - 6 - tickW - 8
    const yTitle = ctx.truncate('Intersection size', barsBottom - top, tick)
    if (titleX - tick > 2 && yTitle) marks.push({ type: 'text', layer: 'axes', key: 'y-title', x: titleX, y: (top + barsBottom) / 2, text: yTitle, anchor: 'middle', baseline: 'auto', rotate: -90, size: tick, fill: theme.muted })

    const totalItems = m.combos.reduce((s, c) => s + c.size, 0)
    const barFill = theme.cat(0)
    // matrix stripes behind every other row
    sets.forEach((s, i) => {
      if (i % 2 === 0) marks.push({ type: 'rect', layer: 'back', key: `stripe:${s.name}`, x: 0, y: matrixTop + i * rowH, width: matrixRight, height: rowH, fill: theme.track, fillOpacity: 0.55, inert: true })
    })
    const dotR = Math.max(2, Math.min(rowH, colW) * 0.3)
    shown.forEach((c, j) => {
      const cx = x0 + j * colW + colW / 2
      const bw = Math.max(1, colW * 0.66)
      const by = y(c.size)
      const name = comboName(c)
      marks.push({
        type: 'rect', role: 'bar', key: `c:${c.key}`, x: cx - bw / 2, y: by, width: bw, height: Math.max(1, barsBottom - by), rx: Math.min(2, bw / 4), fill: barFill,
        datum: c, label: `${name}: ${fmt.value(c.size)}`,
        tooltip: { title: c.sets.join(' ∩ '), rows: [['Only in these sets', fmt.value(c.size)], ['Share of all items', pct(fmt, c.size, totalItems)], ['Sets', String(c.sets.length)]], color: barFill },
      })
      const v = fmt.value(c.size)
      if (o.showValues && ctx.measure(v, tick) <= colW + 2) marks.push({ type: 'text', layer: 'labels', key: `cv:${c.key}`, x: cx, y: by - 4, text: v, anchor: 'middle', baseline: 'alphabetic', size: tick, fill: theme.inkSecondary, inert: true })
      const rows = c.sets.map((s) => rowOf.get(s)!).sort((a, b) => a - b)
      if (rows.length > 1) {
        marks.push({ type: 'line', layer: 'marks', key: `link:${c.key}`, x1: cx, x2: cx, y1: matrixTop + rows[0]! * rowH + rowH / 2, y2: matrixTop + rows[rows.length - 1]! * rowH + rowH / 2, stroke: theme.ink, strokeWidth: Math.max(1.5, dotR * 0.55), inert: true })
      }
      const member = new Set(rows)
      sets.forEach((s, i) => {
        marks.push({ type: 'circle', layer: 'marks', key: `dot:${c.key}:${s.name}`, cx, cy: matrixTop + i * rowH + rowH / 2, r: dotR, fill: member.has(i) ? theme.ink : theme.grid, inert: true })
      })
    })

    // set totals: bars growing left from the names
    const maxSet = Math.max(1, ...sets.map((s) => s.size))
    const valueW = Math.max(...sets.map((s) => ctx.measure(fmt.value(s.size), tick))) + 6
    const barSpace = Math.max(8, setBarW - valueW - 2)
    const setFill = theme.inkSecondary
    sets.forEach((s, i) => {
      const cy = matrixTop + i * rowH + rowH / 2
      const len = Math.max(1, (s.size / maxSet) * barSpace)
      const bh = Math.max(2, rowH * 0.62)
      marks.push({
        type: 'rect', role: 'set', key: `s:${s.name}`, x: setBarW - len, y: cy - bh / 2, width: len, height: bh, rx: 1, fill: setFill,
        datum: s, label: `${s.name}: ${fmt.value(s.size)} in total`,
        tooltip: { title: s.name, rows: [['Set size', fmt.value(s.size)], ['Share of all items', pct(fmt, s.size, totalItems)]], color: setFill },
      })
      marks.push({ type: 'text', layer: 'labels', key: `sv:${s.name}`, x: setBarW - len - 4, y: cy, text: fmt.value(s.size), anchor: 'end', baseline: 'central', size: tick, fill: theme.muted, inert: true })
      const shownName = ctx.truncate(s.name, labelW - 8, labelSize)
      marks.push({ type: 'text', layer: 'labels', key: `sn:${s.name}`, x: x0 - 6, y: cy, text: shownName, full: shownName !== s.name ? s.name : undefined, anchor: 'end', baseline: 'central', size: labelSize, fill: theme.ink })
    })
    const caption = ctx.truncate('Set size', setBarW, tick)
    if (caption) marks.push({ type: 'text', layer: 'axes', key: 'set-caption', x: setBarW - barSpace / 2, y: H - captionH / 2, text: caption, anchor: 'middle', baseline: 'central', size: tick, fill: theme.muted })

    return {
      marks,
      note: notes([
        [m.counts.duplicates, 'duplicate rows summed'],
        [m.counts.repeated, 'repeated rows ignored (that item was already in those sets)'],
        [m.counts.nulls, 'rows without a size left out'],
        [m.counts.negatives, 'negative sizes left out'],
        [m.counts.inconsistent, 'sizes smaller than their listed overlaps (counted as zero)'],
        [m.counts.none, 'items in no set not shown'],
        [droppedSets, 'smallest sets not shown for lack of room'],
        [omitted, 'smaller intersections not shown (all are in the table)'],
      ]),
    }
  },
  describe({ data, options, fmt, name }) {
    const m = intersections(data, options.sizes)
    const top = m.combos[0]
    const big = m.sets[0]
    return `${name}, ${phrase.count(m.sets.length, 'set')}, ${phrase.count(m.combos.length, 'intersection')}${top ? `, largest ${comboName(top)} at ${fmt.value(top.size)}` : ''}${big ? `, largest set ${big.name} at ${fmt.value(big.size)}` : ''}`
  },
  table(data, options) {
    const m = intersections(data, options.sizes)
    return { columns: ['Intersection', 'Sets', 'Size'], rows: m.combos.map((c) => [c.sets.join(' ∩ '), c.sets.length, c.size]) }
  },
})

export default upset
