// Swimlane timeline: one horizontal lane per `lane` value, items as bars inside it on a
// shared time axis. Items that overlap in a lane are packed greedily into sub-rows, so a
// lane is exactly as tall as its busiest moment needs.

import { defineChart, phrase, SIZES } from '../../core'
import type { Formatters, Mark } from '../../core'
import { categoryColors, diamond, fit, laneChrome, layoutLanes, packLanes, paddedDomain, zonedRows } from '../_shared/TimelineA'
import { input, options, type SwimlaneOptions, type SwimlaneRow } from './schema'

const isInstant = (r: SwimlaneRow) => !r.end || +r.end === +r.start

function itemLabel(r: SwimlaneRow, fmt: Formatters): string {
  return isInstant(r) ? `${r.lane}: ${r.label}, ${fmt.dateTime(r.start)}` : `${r.lane}: ${r.label} (${fmt.dateTime(r.start)} – ${fmt.dateTime(r.end)})`
}

/** Lanes in first-seen order with their items. */
function lanesOf(rows: SwimlaneRow[]): Map<string, SwimlaneRow[]> {
  const lanes = new Map<string, SwimlaneRow[]>()
  for (const r of rows) lanes.set(r.lane, [...(lanes.get(r.lane) ?? []), r])
  return lanes
}

export const swimlaneTimeline = defineChart<SwimlaneRow[], SwimlaneOptions>({
  id: 'swimlane-timeline',
  name: 'Swimlane timeline',
  category: 'timeline',
  intents: ['timeline', 'schedule'],
  summary: 'Items as bars in one lane per team, system or person, on a shared time axis.',
  input,
  options,
  shape: 'rows',
  fields: ['lane', 'start', 'end', 'label', 'category'],
  dateFields: ['start', 'end'],
  zoom: 'x',
  aspect: 0.55,
  minHeight: 240,
  maxHeight: 640,
  adapt: (raw, o) => zonedRows(raw, ['start', 'end'], o.timeZone),
  draw(ctx) {
    const { theme, options: o, fmt, width: W } = ctx
    const FS = SIZES.tick
    const byCategory = ctx.data.some((r) => r.category !== undefined)
    const colors = categoryColors(byCategory ? ctx.data.map((r) => r.category) : ctx.data.map((r) => r.lane), theme)
    const keyOf = (r: SwimlaneRow) => (byCategory ? r.category : r.lane)
    const legendOn = byCategory && colors.items.length > 1
    const shown = ctx.data.filter((r) => !(legendOn && r.category !== undefined && ctx.hidden.has(r.category)))
    const lanes = lanesOf(ctx.data)

    const labelW = Math.max(Math.min(56, W * 0.2), Math.min(Math.max(...[...lanes.keys()].map((l) => ctx.measure(l, FS, 600))), W * 0.25))
    const lo = Math.min(...ctx.data.map((r) => +r.start)), hi = Math.max(...ctx.data.map((r) => +(r.end ?? r.start)))
    const f = ctx.cartesian({ x: { type: 'time', domain: paddedDomain(lo, hi, 0.03) }, y: { type: 'linear', domain: [0, 1] }, yAxis: 'none', grid: 'x', zoom: true, pad: { left: labelW + 8 } })
    const { plot } = f
    const marks: Mark[] = [...f.marks]

    // sub-rows: pack each lane's visible items by pixel extent (touching is fine)
    const pxSpan = (r: SwimlaneRow, rr: number) => (isInstant(r) ? { start: f.px(r.start) - rr, end: f.px(r.start) + rr } : { start: f.px(r.start), end: f.px(r.end) })
    const rough = Math.min(7, (plot.height / Math.max(1, ctx.data.length)) * 0.4 + 2)
    const packed = [...lanes.keys()].map((lane) => {
      const items = shown.filter((r) => r.lane === lane)
      const p = packLanes(items.map((r) => pxSpan(r, rough)), -0.5)
      return { lane, items, sub: p.lane, rows: Math.max(1, p.lanes) }
    })
    const laid = layoutLanes(packed.map((p) => ({ key: p.lane, label: p.lane, rows: p.rows })), plot.y, plot.height)
    marks.push(...laneChrome(ctx, laid, plot, 4, labelW))

    packed.forEach((p, li) => {
      const L = laid[li]!
      const barH = Math.max(0.5, Math.min(L.rowH * 0.72, 26))
      p.items.forEach((r, k) => {
        const yc = L.y + L.pad + (p.sub[k]! + 0.5) * L.rowH
        const c = colors.color(keyOf(r)), pat = colors.index(keyOf(r))
        const key = `${r.lane}|${r.label}|${+r.start}|${k}`
        const tip = { title: r.label, color: c, rows: [['Lane', r.lane], [isInstant(r) ? 'At' : 'Start', fmt.dateTime(r.start)], ...(isInstant(r) ? [] : [['End', fmt.dateTime(r.end)]]), ...(r.category !== undefined ? [['Category', r.category]] : [])] as Array<[string, string]> }
        const series = legendOn ? r.category : undefined
        if (isInstant(r)) {
          const x = f.px(r.start), rr = Math.max(1.5, Math.min(barH / 2 + 1, 7))
          marks.push({ type: 'path', role: 'item', key, d: diamond(x, yc, rr), cx: x, cy: yc, box: { x: x - rr, y: yc - rr, width: rr * 2, height: rr * 2 }, fill: c, stroke: theme.surface, strokeWidth: 1, pattern: pat, series, datum: r, label: itemLabel(r, fmt), tooltip: tip })
          return
        }
        const x0 = f.px(r.start), x1 = f.px(r.end)
        // a 1px seam between abutting bars
        const w = Math.max(1, x1 - x0 - (x1 - x0 > 3 ? 1 : 0))
        marks.push({ type: 'rect', role: 'item', key, x: x0, y: yc - barH / 2, width: w, height: barH, rx: Math.min(4, barH / 3, w / 2), fill: theme.mix(c, theme.surface, 0.6), stroke: c, strokeWidth: barH >= 4 ? 1 : 0, pattern: pat, series, datum: r, label: itemLabel(r, fmt), tooltip: tip })
        // label inside the visible part of the bar
        const vx0 = Math.max(x0, plot.x), vx1 = Math.min(x0 + w, plot.x + plot.width)
        if (o.showLabels && barH >= FS + 2 && vx1 - vx0 >= ctx.measure('Abc…', FS) + 8) {
          const t = fit(ctx, r.label, vx1 - vx0 - 8, FS)
          marks.push({ type: 'text', layer: 'labels', key: `l:${key}`, x: vx0 + 4, y: yc, text: t.text, full: t.full, className: t.full ? 'oc-trunc' : undefined, anchor: 'start', baseline: 'central', size: FS, fill: theme.ink })
        }
      })
    })
    return { marks, plot, clip: plot, invertX: f.invertX, legend: legendOn ? { items: colors.items } : undefined }
  },
  describe({ data, fmt, name }) {
    const lanes = lanesOf(data)
    let busiest = ''
    let most = 0
    for (const [lane, items] of lanes) if (items.length > most) { most = items.length; busiest = lane }
    const parts = [`${phrase.count(data.length, 'item')} in ${phrase.count(lanes.size, 'lane')}`, phrase.dateSpan(data.flatMap((r) => (r.end ? [r.start, r.end] : [r.start])), fmt)]
    if (lanes.size > 1) parts.push(`busiest ${busiest} with ${phrase.count(most, 'item')}`)
    return `${name}, ${parts.join(', ')}`
  },
  table(data) {
    return { columns: ['Lane', 'Start', 'End', 'Label', 'Category'], rows: data.map((r) => [r.lane, r.start.toISOString(), r.end ? r.end.toISOString() : null, r.label, r.category ?? null]) }
  },
})

export default swimlaneTimeline
