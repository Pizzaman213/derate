// Roadmap: items as rounded bars in lanes (themes, teams, products) on a quarter axis.
// Quarters follow an optional fiscal-year start; 'YYYY-Qn' and 'FY27 Q2' inputs are read
// in that calendar. Status drives colour, a dashed outline for planned work, and a pattern
// when `patterns` is on.

import { defineChart, phrase, SIZES } from '../../core'
import type { Formatters, LegendItem, Mark, Paint, Palette } from '../../core'
import { fiscalQuarter, fiscalQuarterStart } from '../../time/fiscal'
import { offsetBy } from '../../time/granularity'
import { parseDate } from '../../time/parse'
import { fit, laneChrome, layoutLanes, packLanes } from '../_shared/TimelineA'
import { input, options, type RoadmapOptions, type RoadmapRow } from './schema'

type Kind = 'done' | 'progress' | 'risk' | 'blocked' | 'planned' | 'other' | 'none'
const KINDS: Array<[Exclude<Kind, 'other' | 'none'>, RegExp]> = [
  ['done', /^(done|complete|completed|shipped|released|finished|launched|live)$/i],
  ['progress', /^(in[\s-]?progress|active|doing|ongoing|started|wip|in development|building)$/i],
  ['risk', /^(at[\s-]?risk|risk|delayed|late|slipping)$/i],
  ['blocked', /^(blocked|on[\s-]?hold|stalled|paused)$/i],
  ['planned', /^(planned|todo|to do|not started|backlog|proposed|next|later|future|exploring|idea)$/i],
]
export const statusKind = (s: string | undefined): Kind => (s === undefined || !s.trim() ? 'none' : KINDS.find(([, re]) => re.test(s.trim()))?.[0] ?? 'other')
const NO_STATUS = '(no status)'
const statusKey = (r: RoadmapRow) => (r.status?.trim() || NO_STATUS).toLowerCase()

// 'FY27 Q2', 'FY2027-Q2', '2026-Q1', '2026 Q1' and the quarter-first spellings
const YEAR = String.raw`(?:FY\s*(\d{4}|\d{2})|(\d{4}))`
const YEAR_Q = new RegExp(String.raw`^${YEAR}\s*[-\s/]?\s*Q([1-4])$`, 'i')
const Q_YEAR = new RegExp(String.raw`^Q([1-4])\s*[-\s/]?\s*${YEAR}$`, 'i')
/** 'YYYY-Qn', 'FY27 Q2', 'Q3 2026' -> [fiscal year, quarter], else null. */
export function parseQuarter(v: string): [number, 1 | 2 | 3 | 4] | null {
  const s = v.trim()
  let y: string | undefined, q: string | undefined
  let m = YEAR_Q.exec(s)
  if (m) { y = m[1] ?? m[2]; q = m[3] }
  else if ((m = Q_YEAR.exec(s))) { q = m[1]; y = m[2] ?? m[3] }
  if (!y || !q) return null
  const year = y.length === 2 ? 2000 + Number(y) : Number(y)
  return [year, Number(q) as 1 | 2 | 3 | 4]
}

const startMonthOf = (o: { fiscalYearStart?: unknown }) => (typeof o.fiscalYearStart === 'number' && Number.isInteger(o.fiscalYearStart) && o.fiscalYearStart >= 1 && o.fiscalYearStart <= 12 ? o.fiscalYearStart : 1)

/** phase/theme aliases, quarter strings in the fiscal calendar, zone-aware dates. */
function adaptRows(raw: unknown, o: { fiscalYearStart?: unknown; timeZone?: string }): unknown {
  if (!Array.isArray(raw)) return raw
  const cal = { startMonth: startMonthOf(o), timeZone: o.timeZone ?? 'UTC' }
  const when = (v: unknown, isEnd: boolean): unknown => {
    if (typeof v !== 'string') return v
    const q = parseQuarter(v)
    if (q) {
      const s = fiscalQuarterStart(q[0], q[1], cal)
      return isEnd ? offsetBy(s, 'month', 3, cal) : s
    }
    return parseDate(v, { timeZone: cal.timeZone }) ?? v
  }
  return raw.map((r: unknown) => {
    if (!r || typeof r !== 'object' || Array.isArray(r)) return r
    const o2: Record<string, unknown> = { ...(r as Record<string, unknown>) }
    if (o2.item === undefined && o2.phase !== undefined) o2.item = o2.phase
    if (o2.lane === undefined && o2.theme !== undefined) o2.lane = o2.theme
    o2.start = when(o2.start, false)
    o2.end = when(o2.end, true)
    return o2
  })
}

export interface Item {
  row: RoadmapRow
  start: Date
  end: Date
  /** End was missing and was set to the end of the start quarter. */
  filled: boolean
  kind: Kind
}

/** Quarter that contains `d` in the fiscal calendar: [start, next start). */
function quarterOf(d: Date, cal: { startMonth: number; timeZone: string }): [Date, Date] {
  const fq = fiscalQuarter(d, cal)
  const s = fiscalQuarterStart(fq.fiscalYear, fq.quarter, cal)
  return [s, offsetBy(s, 'month', 3, cal)]
}

function itemsOf(rows: RoadmapRow[], startMonth: number, timeZone: string): Item[] {
  const cal = { startMonth, timeZone }
  return rows.map((row) => ({ row, start: row.start, end: row.end ?? quarterOf(row.start, cal)[1], filled: !row.end, kind: statusKind(row.status) }))
}

/** Paint and legend per status spelling (first-seen spelling wins); kinds ordered done -> planned. */
function statusStyle(items: Item[], theme: Palette): { paint: (it: Item) => Paint; pattern: (it: Item) => number; legend: LegendItem[] } {
  const anyStatus = items.some((it) => it.kind !== 'none')
  const base: Record<Kind, Paint> = { done: theme.positive, progress: theme.cat(0), risk: theme.warning, blocked: theme.negative, planned: theme.neutral, other: theme.cat(1), none: anyStatus ? theme.muted : theme.cat(0) }
  const order: Kind[] = ['done', 'progress', 'risk', 'blocked', 'planned', 'other', 'none']
  const seen = new Map<string, { label: string; kind: Kind }>()
  for (const it of items) {
    const k = statusKey(it.row)
    if (!seen.has(k)) seen.set(k, { label: it.row.status?.trim() || NO_STATUS, kind: it.kind })
  }
  const entries = [...seen.entries()].sort((a, b) => order.indexOf(a[1].kind) - order.indexOf(b[1].kind))
  let other = 0
  const paint = new Map<string, Paint>()
  const pat = new Map<string, number>()
  const perKind = new Map<Kind, number>()
  entries.forEach(([k, e], i) => {
    // a second spelling of the same kind ('Planned', 'Exploring') gets a shade of its colour
    const n = perKind.get(e.kind) ?? 0
    perKind.set(e.kind, n + 1)
    paint.set(k, e.kind === 'other' ? theme.cat(1 + other++) : n ? theme.mix(base[e.kind], theme.ink, Math.min(0.6, 0.3 * n)) : base[e.kind])
    pat.set(k, i)
  })
  return {
    paint: (it) => paint.get(statusKey(it.row)) ?? base.other,
    pattern: (it) => pat.get(statusKey(it.row)) ?? 0,
    legend: entries.map(([k, e]) => ({ key: k, label: e.label, color: paint.get(k)!, shape: 'square', pattern: pat.get(k) })),
  }
}

function quarterName(d: Date, startMonth: number, timeZone: string, fmt: Formatters): string {
  return startMonth === 1 ? fmt.date(d, 'quarter') : fiscalQuarter(d, { startMonth, timeZone }).label
}

export const roadmap = defineChart<RoadmapRow[], RoadmapOptions>({
  id: 'roadmap',
  name: 'Roadmap',
  category: 'timeline',
  intents: ['schedule', 'timeline'],
  summary: 'Items as rounded bars in lanes on a quarter axis, coloured by status.',
  input,
  options,
  shape: 'rows',
  fields: ['item', 'lane', 'start', 'end', 'status'],
  dateFields: ['start', 'end'],
  zoom: 'x',
  aspect: 0.5,
  minHeight: 240,
  maxHeight: 600,
  adapt: (raw, o) => adaptRows(raw, o),
  draw(ctx) {
    const { theme, options: o, fmt, width: W } = ctx
    const FS = SIZES.tick
    const tz = fmt.timeZone
    const cal = { startMonth: o.fiscalYearStart, timeZone: tz }
    const all = itemsOf(ctx.data, o.fiscalYearStart, tz)
    const style = statusStyle(all, theme)
    const legendOn = all.some((it) => it.kind !== 'none') && style.legend.length > 1
    const items = all.filter((it) => !(legendOn && ctx.hidden.has(statusKey(it.row))))
    const lanes = [...new Set(all.map((it) => it.row.lane))]

    // quarter-snapped domain
    const q0 = quarterOf(new Date(Math.min(...all.map((it) => +it.start))), cal)[0]
    const last = Math.max(...all.map((it) => +it.end))
    const quarters: Date[] = [q0]
    while (+quarters[quarters.length - 1]! < last || quarters.length < 2) quarters.push(offsetBy(quarters[quarters.length - 1]!, 'month', 3, cal))
    const rowH = FS + 8
    const labelW = Math.max(Math.min(56, W * 0.2), Math.min(Math.max(...lanes.map((l) => ctx.measure(l, FS, 600))), W * 0.25))
    const f = ctx.cartesian({ x: { type: 'time', domain: [quarters[0]!, quarters[quarters.length - 1]!] }, y: { type: 'linear', domain: [0, 1] }, xAxis: 'none', yAxis: 'none', grid: 'none', zoom: true, pad: { top: rowH * 2, left: labelW + 8, bottom: 4 } })
    const { plot } = f
    const marks: Mark[] = [...f.marks]
    const inX = (x: number) => x >= plot.x - 0.5 && x <= plot.x + plot.width + 0.5

    // header: fiscal years over quarters, then the quarter cells
    const yTop = plot.y - rowH * 2, qRow = plot.y - rowH
    marks.push({ type: 'line', layer: 'axes', key: 'hdr-base', x1: plot.x, x2: plot.x + plot.width, y1: plot.y, y2: plot.y, stroke: theme.axis, strokeWidth: 1, inert: true })
    const qw = ctx.measure('Q4', FS) + 8
    const years = new Map<number, [number, number]>()
    for (let i = 0; i < quarters.length - 1; i++) {
      const a = quarters[i]!, b = quarters[i + 1]!
      const fq = fiscalQuarter(a, cal)
      const x0 = Math.max(plot.x, f.px(a)), x1 = Math.min(plot.x + plot.width, f.px(b))
      if (x1 <= x0) continue
      const yr = years.get(fq.fiscalYear)
      years.set(fq.fiscalYear, yr ? [Math.min(yr[0], x0), Math.max(yr[1], x1)] : [x0, x1])
      if (x1 - x0 >= qw) marks.push({ type: 'text', layer: 'axes', key: `q:${+a}`, x: (x0 + x1) / 2, y: qRow + rowH / 2, text: `Q${fq.quarter}`, anchor: 'middle', baseline: 'central', size: FS, fill: theme.inkSecondary })
      if (i % 2 === 1) marks.push({ type: 'rect', layer: 'back', key: `qbg:${+a}`, x: x0, y: qRow, width: x1 - x0, height: rowH + plot.height, fill: theme.track, fillOpacity: 0.25, inert: true })
    }
    for (const q of quarters) {
      const x = f.px(q)
      if (!inX(x)) continue
      const yearEdge = fiscalQuarter(q, cal).quarter === 1
      marks.push({ type: 'line', layer: 'grid', key: `qg:${+q}`, x1: x, x2: x, y1: yearEdge ? yTop : qRow, y2: plot.y + plot.height, stroke: yearEdge ? theme.axis : theme.grid, strokeWidth: 1, inert: true })
    }
    for (const [fy, [x0, x1]] of years) {
      const name = o.fiscalYearStart === 1 ? String(fy) : `FY${String(fy % 100).padStart(2, '0')}`
      const t = fit(ctx, name, x1 - x0 - 6, FS)
      if (t.text && !t.full) marks.push({ type: 'text', layer: 'axes', key: `fy:${fy}`, x: (x0 + x1) / 2, y: yTop + rowH / 2, text: t.text, anchor: 'middle', baseline: 'central', size: FS, weight: 600, fill: theme.ink })
    }

    // lanes, sub-rows by pixel extent
    const packed = lanes.map((lane) => {
      const its = items.filter((it) => it.row.lane === lane)
      // bars are inset 1px each side, so items that abut share a sub-row
      const p = packLanes(its.map((it) => ({ start: f.px(it.start), end: Math.max(f.px(it.end), f.px(it.start) + 2) })), -0.5)
      return { lane, its, sub: p.lane, rows: Math.max(1, p.lanes) }
    })
    const laid = layoutLanes(packed.map((p) => ({ key: p.lane, label: p.lane, rows: p.rows })), plot.y, plot.height)
    marks.push(...laneChrome(ctx, laid, plot, 4, labelW))

    packed.forEach((p, li) => {
      const L = laid[li]!
      const barH = Math.max(0.5, Math.min(L.rowH * 0.7, 24))
      p.its.forEach((it, k) => {
        const yc = L.y + L.pad + (p.sub[k]! + 0.5) * L.rowH
        const c = style.paint(it)
        const x0 = f.px(it.start), w = Math.max(2, f.px(it.end) - x0 - 2)
        const status = it.row.status?.trim()
        const span = `${quarterName(it.start, o.fiscalYearStart, tz, fmt)} – ${quarterName(new Date(+it.end - 1), o.fiscalYearStart, tz, fmt)}`
        const planned = it.kind === 'planned'
        marks.push({
          type: 'rect', role: 'item', key: `${it.row.lane}|${it.row.item}|${k}`, x: x0 + 1, y: yc - barH / 2, width: w, height: barH, rx: Math.min(barH / 2, 8, w / 2),
          fill: theme.mix(c, theme.surface, planned ? 0.8 : 0.55), stroke: c, strokeWidth: barH >= 4 ? 1.5 : 0, dash: planned ? '4 3' : undefined, pattern: style.pattern(it),
          series: legendOn ? statusKey(it.row) : undefined, datum: it.row,
          label: `${it.row.lane}: ${it.row.item} (${span})${status ? `, ${status}` : ''}`,
          tooltip: { title: it.row.item, color: c, rows: [['Lane', it.row.lane], ['Start', fmt.dateTime(it.start)], ['End', it.filled ? `${fmt.dateTime(it.end)} (end of quarter)` : fmt.dateTime(it.end)], ['Quarters', span], ...(status ? [['Status', status]] : [])] as Array<[string, string]> },
        })
        const vx0 = Math.max(x0 + 1, plot.x), vx1 = Math.min(x0 + 1 + w, plot.x + plot.width)
        const inset = Math.min(barH / 2, 8)
        if (o.showLabels && barH >= FS + 2 && vx1 - vx0 - inset * 2 >= ctx.measure('Abc…', FS)) {
          const t = fit(ctx, it.row.item, vx1 - vx0 - inset * 2, FS)
          marks.push({ type: 'text', layer: 'labels', key: `l:${it.row.lane}|${it.row.item}|${k}`, x: vx0 + inset, y: yc, text: t.text, full: t.full, className: t.full ? 'oc-trunc' : undefined, anchor: 'start', baseline: 'central', size: FS, fill: theme.ink })
        }
      })
    })
    const filled = items.filter((it) => it.filled).length
    return {
      marks, plot, clip: plot, invertX: f.invertX,
      legend: legendOn ? { items: style.legend } : undefined,
      note: filled ? `${filled} items without an end run to the end of their start quarter` : undefined,
    }
  },
  describe({ data, options: o, fmt, name }) {
    const items = itemsOf(data, o.fiscalYearStart, fmt.timeZone)
    const lanes = new Set(data.map((r) => r.lane)).size
    const first = new Date(Math.min(...items.map((it) => +it.start))), last = new Date(Math.max(...items.map((it) => +it.end)) - 1)
    const parts = [`${phrase.count(items.length, 'item')} in ${phrase.count(lanes, 'lane')}`, `${quarterName(first, o.fiscalYearStart, fmt.timeZone, fmt)} to ${quarterName(last, o.fiscalYearStart, fmt.timeZone, fmt)}`]
    const counts = new Map<string, number>()
    for (const it of items) if (it.row.status) counts.set(it.row.status.trim().toLowerCase(), (counts.get(it.row.status.trim().toLowerCase()) ?? 0) + 1)
    if (counts.size) parts.push([...counts].map(([s, n]) => `${n} ${s}`).join(', '))
    return `${name}, ${parts.join(', ')}`
  },
  table(data, o, fmt) {
    const items = itemsOf(data, o.fiscalYearStart, fmt.timeZone)
    return { columns: ['Lane', 'Item', 'Start', 'End', 'Status'], rows: items.map((it) => [it.row.lane, it.row.item, it.start.toISOString(), it.end.toISOString(), it.row.status ?? null]) }
  },
})

export default roadmap
