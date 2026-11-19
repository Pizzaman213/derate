// Shared by venn, euler and upset: the set-overlap input ({ sets: 'A,B', size } rows or
// venn.js's { sets: [{ sets: ['A'], size }] }), its collapse into inclusive/exclusive
// sizes, circle geometry, and the circle-diagram drawing venn and euler both use.

import { z } from 'zod'
import { intersectionAreaPath } from '@upsetjs/venn.js'
import { fields, SIZES, phrase } from '../../core'
import type { DrawContext, DrawResult, Formatters, LegendItem, Mark, TableModel } from '../../core'
import { AdapterError, isRecord, mapFields, toRows } from '../../adapters'
import { notes } from '../../core/helpers'

// ---------------------------------------------------------------------------------------
// Input

/** 'A,B' / 'A;B' / 'A|B' / ['A', 'B'] -> unique trimmed names; anything else is left for zod to reject. */
export function splitSets(v: unknown): unknown {
  if (v === null || v === undefined) return []
  if (typeof v === 'number') return [String(v)]
  if (typeof v === 'string') return uniqNames(v.split(/[,;|]/))
  if (Array.isArray(v)) {
    const out = v.map((x) => (typeof x === 'number' ? String(x) : x))
    return out.every((x): x is string => typeof x === 'string') ? uniqNames(out) : out
  }
  return v
}
const uniqNames = (parts: string[]) => [...new Set(parts.map((s) => s.trim()).filter(Boolean))]

/** A list of set names; `min` 0 lets a membership row say "in no set". */
export const setList = (description: string, min = 1) => z.preprocess(splitSets, z.array(z.string({ invalid_type_error: 'text' })).min(min)).describe(description)

const areaRow = z.object({
  sets: setList("the sets this size belongs to: 'A' for a set, 'A,B' for the overlap of A and B"),
  size: fields.numOrNull('items in every one of `sets` (inclusive, venn.js convention); null leaves it out'),
}).passthrough()

export type AreaRow = z.output<typeof areaRow>

/** Rows of { sets, size } with a cap on the number of distinct sets. */
export function areasInput(maxSets: number, tooMany: (n: number, names: string[]) => string) {
  return z.array(areaRow).superRefine((rows, ctx) => {
    const names = new Set<string>()
    const own = new Set<string>()
    for (const r of rows) {
      for (const s of r.sets) names.add(s)
      if (r.sets.length === 1) own.add(r.sets[0]!)
    }
    if (names.size > maxSets) ctx.addIssue({ code: z.ZodIssueCode.custom, message: tooMany(names.size, [...names]), path: [] })
    const orphans = [...names].filter((n) => !own.has(n))
    if (orphans.length) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom, path: [],
        message: `${orphans.length === 1 ? 'set' : 'sets'} ${orphans.map((n) => `'${n}'`).join(', ')} ${orphans.length === 1 ? 'appears' : 'appear'} in an overlap but ${orphans.length === 1 ? 'has' : 'have'} no size of ${orphans.length === 1 ? 'its' : 'their'} own: add a row like { sets: '${orphans[0]}', size: 120 }`,
      })
    }
  }).describe('rows of { sets, size }')
}

/** venn.js's { sets: [...] } wrapper, rows, CSV text or column arrays -> rows (field mapping applied). */
export function adaptAreas(raw: unknown, opts: { fields?: Record<string, string> }, rowShape = '{ sets, size }'): unknown {
  if (raw === null || raw === undefined) return []
  if (isRecord(raw) && Array.isArray(raw.sets) && raw.sets.every(isRecord)) return raw.sets
  const rows = toRows(raw)
  if (rows === null) throw new AdapterError(`the data must be a list of ${rowShape} rows, CSV text, column arrays, or { sets: [{ sets: ['A'], size }] }, got ${typeof raw === 'string' ? 'text without a header row' : Array.isArray(raw) ? 'a list that is not rows' : typeof raw}`)
  return opts.fields && Object.keys(opts.fields).length ? mapFields(rows, opts.fields) : rows
}

// ---------------------------------------------------------------------------------------
// Model

export interface Area {
  /** Member set names, in set order. */
  sets: string[]
  /** Inclusive size: items in every one of `sets`. */
  size: number
}

export interface SetModel {
  /** Drawable sets (positive size), input order. */
  names: string[]
  /** Singletons first, then overlaps by degree, each in set order. */
  areas: Area[]
  /** Inclusive size by area key. */
  sizes: Map<string, number>
  counts: { nulls: number; negatives: number; zeros: number; duplicates: number; orphans: number; clamped: number }
}

/** Order-free identity of a combination; '|' never occurs in a name because it separates them on input. */
export const areaKey = (sets: readonly string[]) => [...sets].sort().join('|')
export const areaLabel = (sets: readonly string[]) => sets.join(' ∩ ')

/** Sum duplicates, drop nulls/negatives/empty sets (counted), clamp overlaps to their members. */
export function collapseAreas(rows: readonly AreaRow[], hidden: ReadonlySet<string> = new Set()): SetModel {
  const counts = { nulls: 0, negatives: 0, zeros: 0, duplicates: 0, orphans: 0, clamped: 0 }
  const merged = new Map<string, { sets: string[]; size: number }>()
  for (const r of rows) {
    if (r.size === null) { counts.nulls++; continue }
    if (r.size < 0) { counts.negatives++; continue }
    const k = areaKey(r.sets)
    const hit = merged.get(k)
    if (hit) { counts.duplicates++; hit.size += r.size } else merged.set(k, { sets: r.sets, size: r.size })
  }
  const names: string[] = []
  const sizes = new Map<string, number>()
  for (const a of merged.values()) {
    if (a.sets.length !== 1) continue
    if (a.size <= 0) { counts.zeros++; continue }
    if (hidden.has(a.sets[0]!)) continue
    names.push(a.sets[0]!)
    sizes.set(areaKey(a.sets), a.size)
  }
  const order = new Map(names.map((n, i) => [n, i]))
  const overlaps: Area[] = []
  for (const a of merged.values()) {
    if (a.sets.length < 2) continue
    if (a.sets.some((s) => hidden.has(s))) continue
    if (a.sets.some((s) => !order.has(s))) { if (a.size > 0) counts.orphans++; continue }
    const sets = [...a.sets].sort((x, y) => order.get(x)! - order.get(y)!)
    const cap = Math.min(...sets.map((s) => sizes.get(s)!))
    let size = a.size
    if (size > cap) { counts.clamped++; size = cap }
    overlaps.push({ sets, size })
  }
  overlaps.sort((a, b) => a.sets.length - b.sets.length || order.get(a.sets[0]!)! - order.get(b.sets[0]!)!)
  for (const o of overlaps) sizes.set(areaKey(o.sets), o.size)
  const areas: Area[] = [...names.map((n) => ({ sets: [n], size: sizes.get(n)! })), ...overlaps]
  return { names, areas, sizes, counts }
}

/** Items in exactly `sets` and no other drawn set, by inclusion-exclusion (unlisted overlaps are empty). */
export function exclusiveSize(model: SetModel, sets: readonly string[]): number {
  const own = new Set(sets)
  let total = 0
  for (const a of model.areas) {
    if (a.sets.length < own.size || !sets.every((s) => a.sets.includes(s))) continue
    total += (a.sets.length - own.size) % 2 === 0 ? a.size : -a.size
  }
  return total
}

export function modelNotes(model: SetModel, extra: Array<[number, string]> = []): string | undefined {
  const c = model.counts
  return notes([
    [c.duplicates, 'duplicate rows summed'],
    [c.nulls, 'rows without a size left out'],
    [c.negatives, 'negative sizes left out'],
    [c.zeros, 'empty sets not drawn'],
    [c.orphans, 'overlaps of left-out sets not drawn'],
    [c.clamped, 'overlaps larger than a member set clamped to it'],
    ...extra,
  ])
}

export function setsEmpty(rows: readonly AreaRow[]): boolean | string {
  if (rows.length === 0) return true
  const singles = rows.filter((r) => r.sets.length === 1 && r.size !== null)
  if (!singles.length) return 'No set sizes to draw'
  if (singles.every((r) => (r.size ?? 0) <= 0)) return 'All set sizes are zero'
  return false
}

export function setsDescribe(rows: readonly AreaRow[], fmt: Formatters, name: string): string {
  const m = collapseAreas(rows)
  const singles = m.areas.filter((a) => a.sets.length === 1)
  const overlaps = m.areas.filter((a) => a.sets.length > 1 && a.size > 0)
  let biggest = singles[0]!
  for (const a of singles) if (a.size > biggest.size) biggest = a
  let top: Area | undefined
  for (const a of overlaps) if (!top || a.size > top.size) top = a
  const largest = singles.length ? `, largest ${biggest.sets[0]} at ${fmt.value(biggest.size)}` : ''
  const over = top ? `, largest overlap ${phrase.listPhrase(top.sets, 8)} at ${fmt.value(top.size)}` : ', no overlaps'
  return `${name}, ${phrase.count(m.names.length, 'set')} (${phrase.listPhrase(m.names)})${largest}${over}`
}

/** True when every listed area has a non-negative exclusive size, i.e. the unlisted overlaps really can be empty. */
export function exclusiveKnown(model: SetModel): boolean {
  return model.areas.every((a) => exclusiveSize(model, a.sets) >= -1e-9)
}

export function setsTable(rows: readonly AreaRow[]): TableModel {
  const m = collapseAreas(rows)
  const known = exclusiveKnown(m)
  return { columns: ['Sets', 'Size', 'Only these sets'], rows: m.areas.map((a) => [areaLabel(a.sets), a.size, known ? exclusiveSize(m, a.sets) : null]) }
}

// ---------------------------------------------------------------------------------------
// Circle geometry

export interface Circle { set: string; x: number; y: number; r: number }

/** Area of the lens where two circles with centres `d` apart overlap. */
export function overlapArea(r1: number, r2: number, d: number): number {
  if (d >= r1 + r2) return 0
  if (d <= Math.abs(r1 - r2)) return Math.PI * Math.min(r1, r2) ** 2
  const a = r1 * r1 * Math.acos((d * d + r1 * r1 - r2 * r2) / (2 * d * r1))
  const b = r2 * r2 * Math.acos((d * d + r2 * r2 - r1 * r1) / (2 * d * r2))
  const c = 0.5 * Math.sqrt(Math.max(0, (-d + r1 + r2) * (d + r1 - r2) * (d - r1 + r2) * (d + r1 + r2)))
  return a + b - c
}

/** Centre distance at which two circles overlap by exactly `area` (bisection; area is monotone in d). */
export function distanceForOverlap(r1: number, r2: number, area: number): number {
  const full = Math.PI * Math.min(r1, r2) ** 2
  if (area <= 0) return r1 + r2
  if (area >= full) return Math.abs(r1 - r2)
  let lo = Math.abs(r1 - r2), hi = r1 + r2
  for (let i = 0; i < 100 && hi - lo > 1e-12 * (r1 + r2); i++) {
    const mid = (lo + hi) / 2
    if (overlapArea(r1, r2, mid) > area) lo = mid
    else hi = mid
  }
  return (lo + hi) / 2
}

/** Uniformly scale and centre circles into a w x h box with `pad` on every side. */
export function fitCircles(circles: Circle[], w: number, h: number, pad: number): Circle[] {
  if (!circles.length) return []
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity
  for (const c of circles) {
    x0 = Math.min(x0, c.x - c.r); x1 = Math.max(x1, c.x + c.r)
    y0 = Math.min(y0, c.y - c.r); y1 = Math.max(y1, c.y + c.r)
  }
  const k = Math.min((w - 2 * pad) / Math.max(1e-9, x1 - x0), (h - 2 * pad) / Math.max(1e-9, y1 - y0))
  const ox = (w - (x1 - x0) * k) / 2 - x0 * k, oy = (h - (y1 - y0) * k) / 2 - y0 * k
  return circles.map((c) => ({ set: c.set, x: c.x * k + ox, y: c.y * k + oy, r: c.r * k }))
}

const maskAt = (circles: readonly Circle[], x: number, y: number): number => {
  let m = 0
  circles.forEach((c, i) => { if ((x - c.x) ** 2 + (y - c.y) ** 2 < c.r * c.r) m |= 1 << i })
  return m
}

interface RegionSample { x: number; y: number; margin: number; x0: number; y0: number; x1: number; y1: number }

/** Sample a grid over the circles: per membership mask, the point deepest inside it and its extent. */
export function sampleRegions(circles: readonly Circle[], w: number, h: number): Map<number, RegionSample> {
  const out = new Map<number, RegionSample>()
  const step = Math.max(1.5, Math.min(w, h) / 120)
  for (let y = step / 2; y < h; y += step) {
    for (let x = step / 2; x < w; x += step) {
      let m = 0, margin = Infinity
      circles.forEach((c, i) => {
        const d = Math.hypot(x - c.x, y - c.y)
        if (d < c.r) m |= 1 << i
        margin = Math.min(margin, Math.abs(d - c.r))
      })
      if (m === 0) continue
      const hit = out.get(m)
      if (!hit) out.set(m, { x, y, margin, x0: x, y0: y, x1: x, y1: y })
      else {
        if (margin > hit.margin) { hit.x = x; hit.y = y; hit.margin = margin }
        hit.x0 = Math.min(hit.x0, x); hit.x1 = Math.max(hit.x1, x)
        hit.y0 = Math.min(hit.y0, y); hit.y1 = Math.max(hit.y1, y)
      }
    }
  }
  return out
}

/** True when a w x h box centred on (x, y), plus breathing room, lies inside membership region `mask` and the frame. */
function boxInRegion(circles: readonly Circle[], mask: number, x: number, y: number, w0: number, h0: number, W: number, H: number): boolean {
  const w = w0 + 12, h = h0 + 4
  if (x - w / 2 < 1 || x + w / 2 > W - 1 || y - h / 2 < 1 || y + h / 2 > H - 1) return false
  for (const fx of [-0.5, -0.25, 0, 0.25, 0.5]) for (const fy of [-0.5, 0, 0.5]) if (maskAt(circles, x + fx * w, y + fy * h) !== mask) return false
  return true
}

// ---------------------------------------------------------------------------------------
// Drawing (venn and euler)

export interface DiagramLayout {
  /** Circles in data units (radius = sqrt(size / pi)), in `model.names` order. */
  circles: Circle[]
  note?: string
}

/** Circles, overlap regions and region labels for a laid-out set diagram. */
export function drawSetDiagram(ctx: DrawContext<AreaRow[], Record<string, unknown>>, layout: (m: SetModel, fmt: Formatters) => DiagramLayout): DrawResult {
  const { theme, fmt, width: W, height: H } = ctx
  const all = collapseAreas(ctx.data)
  const model = collapseAreas(ctx.data, ctx.hidden)
  const colorIndex = new Map(all.names.map((n, i) => [n, i]))
  const legendItems: LegendItem[] = all.names.map((n) => ({ key: n, label: n, color: theme.cat(colorIndex.get(n)!), shape: 'circle', pattern: colorIndex.get(n)! }))
  const legend = all.names.length > 1 ? { items: legendItems } : undefined
  if (!model.names.length) return { marks: [], legend, note: 'Every set is hidden' }

  const laid = layout(model, fmt)
  const circles = fitCircles(laid.circles, W, H, 4)
  const byName = new Map(circles.map((c) => [c.set, c]))
  const marks: Mark[] = []
  const nameSize = SIZES.label, countSize = SIZES.label
  const known = exclusiveKnown(model)

  model.names.forEach((n) => {
    const c = byName.get(n)!
    const i = colorIndex.get(n)!
    const size = model.sizes.get(n)!
    const only = exclusiveSize(model, [n])
    const fill = theme.cat(i)
    marks.push({
      type: 'circle', role: 'set', key: `set:${n}`, cx: c.x, cy: c.y, r: c.r, series: n,
      fill, fillOpacity: 0.22, stroke: fill, strokeWidth: 1.5, pattern: i,
      datum: { sets: [n], size }, label: `${n}: ${fmt.value(size)}`,
      tooltip: { title: n, rows: known ? [['Size', fmt.value(size)], [`Only ${n}`, fmt.value(only)]] : [['Size', fmt.value(size)]], color: fill },
    })
  })

  const regions = sampleRegions(circles, W, H)
  const bit = new Map(circles.map((c, i) => [c.set, 1 << i]))
  const maskOf = (sets: readonly string[]) => sets.reduce((m, s) => m | bit.get(s)!, 0)
  let undrawn = 0
  for (const a of model.areas) {
    if (a.sets.length < 2 || a.size <= 0) continue
    const cs = a.sets.map((s) => byName.get(s)!)
    const d = intersectionAreaPath(cs.map((c) => ({ x: c.x, y: c.y, radius: c.r, setid: c.set }))).trim()
    const want = maskOf(a.sets)
    let anchor: RegionSample | undefined
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity
    for (const [m, s] of regions) {
      if ((m & want) !== want) continue
      if (!anchor || s.margin > anchor.margin) anchor = s
      x0 = Math.min(x0, s.x0); y0 = Math.min(y0, s.y0); x1 = Math.max(x1, s.x1); y1 = Math.max(y1, s.y1)
    }
    if (d === 'M 0 0' || !anchor) { undrawn++; continue }
    const label = areaLabel(a.sets)
    marks.push({
      type: 'path', role: 'intersection', key: `int:${areaKey(a.sets)}`, d, fill: theme.ink, fillOpacity: 0, stroke: 'none',
      cx: anchor.x, cy: anchor.y, box: { x: x0, y: y0, width: Math.max(1, x1 - x0), height: Math.max(1, y1 - y0) },
      datum: { sets: a.sets, size: a.size }, label: `${phrase.listPhrase(a.sets, 8)}: ${fmt.value(a.size)} in common`,
      tooltip: { title: label, rows: known ? [['In all of these', fmt.value(a.size)], ['In only these', fmt.value(exclusiveSize(model, a.sets))]] : [['In all of these', fmt.value(a.size)]] },
    })
  }

  // Region labels. When the listed sizes pin down every region (unlisted overlaps empty),
  // each drawn region gets its exclusive count; otherwise each listed area gets its own
  // (inclusive) size in the region that is exactly it, as venn.js labels them.
  const labels: Array<{ sets: string[]; mask: number; at: RegionSample; value: number }> = []
  if (known) {
    for (const [mask, at] of regions) {
      const sets = model.names.filter((n) => mask & bit.get(n)!)
      const value = exclusiveSize(model, sets)
      if (value > 0) labels.push({ sets, mask, at, value })
    }
  } else {
    for (const a of model.areas) {
      const mask = maskOf(a.sets)
      const at = regions.get(mask)
      if (at && a.size > 0) labels.push({ sets: a.sets, mask, at, value: a.size })
    }
  }
  const lineH = Math.max(nameSize, countSize) * 1.25
  for (const { sets, mask, at: s, value } of labels) {
    const countText = fmt.value(value)
    const cw = ctx.measure(countText, countSize) + 4
    const key = `lab:${areaKey(sets)}`
    if (sets.length === 1) {
      const n = sets[0]!
      let placed = false
      for (const k of [3.2, 2.4, 1.8, 1.2]) {
        const shown = ctx.truncate(n, s.margin * k, nameSize)
        if (!shown || shown === '…') break
        const nw = ctx.measure(shown, nameSize, 600) + 4
        if (!boxInRegion(circles, mask, s.x, s.y, Math.max(nw, cw), lineH * 2, W, H)) continue
        marks.push({ type: 'text', layer: 'labels', key: `${key}:n`, x: s.x, y: s.y - lineH / 2, text: shown, full: shown !== n ? n : undefined, anchor: 'middle', baseline: 'central', size: nameSize, weight: 600, fill: theme.ink, halo: true, inert: shown === n })
        marks.push({ type: 'text', layer: 'labels', key, x: s.x, y: s.y + lineH / 2, text: countText, anchor: 'middle', baseline: 'central', size: countSize, fill: theme.inkSecondary, halo: true, inert: true })
        placed = true
        break
      }
      if (placed) continue
    }
    if (boxInRegion(circles, mask, s.x, s.y, cw, lineH, W, H)) {
      marks.push({ type: 'text', layer: 'labels', key, x: s.x, y: s.y, text: countText, anchor: 'middle', baseline: 'central', size: countSize, fill: sets.length === 1 ? theme.inkSecondary : theme.ink, weight: sets.length > 1 ? 600 : undefined, halo: true, inert: true })
    }
  }

  const note = [modelNotes(model, [[undrawn, 'listed overlaps the layout could not draw (see the table)']]), known ? undefined : 'labels show each listed size, deeper overlaps included, because not every overlap is listed', laid.note].filter(Boolean).join('; ')
  return { marks, legend, note: note || undefined }
}
