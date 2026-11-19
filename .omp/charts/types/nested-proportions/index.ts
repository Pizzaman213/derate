// Nested proportions: a part drawn inside its whole as squares sharing a corner, area
// proportional to value ("of 100 applicants, 40 interviewed, 12 hired"). A whole with
// several parts gets one panel per part, the shared whole repeated as context.

import { defineChart, phrase, SIZES } from '../../core'
import type { Mark, Palette } from '../../core'
import { notes } from '../../core/helpers'
import { cellText, mergeSiblings, toTree, treeFromRows, truncateBold, type TreeInput } from '../_shared/PartWholeB'
import type { Row } from '../../adapters'
import { input, options, type NestedData, type NestedOptions } from './schema'

export interface NNode {
  id: string
  name: string
  value: number
  depth: number
  parent: NNode | null
  children: NNode[]
}

export interface Nested {
  wholes: NNode[]
  /** Root-to-leaf chains; each is drawn as one panel of nested squares. */
  panels: NNode[][]
  negatives: number
  zeros: number
  nulls: number
  merged: number
  grouped: number
  /** Tail parts left out because, summed, they would exceed their whole (overlapping parts). */
  hiddenParts: number
  larger: number
}

/** label/value rows with no parent column read as a chain: each row is a part of the row before it. */
export function nestedFromRows(rows: readonly Row[], separator: string): TreeInput {
  const cols = new Set(rows.flatMap((r) => Object.keys(r)))
  const labelCol = cols.has('label') ? 'label' : cols.has('name') ? 'name' : null
  if (cols.has('path') || cols.has('id') || labelCol === null || rows.some((r) => cellText(r.parent) !== null)) return treeFromRows(rows, separator)
  return treeFromRows(rows.map((r, i) => ({ ...r, parent: i > 0 ? cellText(rows[i - 1]![labelCol]) ?? '(blank)' : null })), separator)
}

/**
 * Own values (a node without one is the sum of its parts); what has no area is dropped and
 * counted, and its parts move up to its own whole (a part of a part is a part of the whole).
 */
export function nest(t: TreeInput, maxParts = 8): Nested {
  const count = { merged: t.merged ?? 0 }
  const tree = mergeSiblings(t, count)
  const out: Nested = { wholes: [], panels: [], negatives: 0, zeros: 0, nulls: 0, merged: count.merged, grouped: 0, hiddenParts: 0, larger: 0 }
  const build = (n: TreeInput, path: string[]): NNode[] => {
    const own = n.value ?? null
    let kids = (n.children ?? []).flatMap((c) => build(c, [...path, c.name]))
    if (own !== null && own < 0) { out.negatives++; return kids }
    if (own === 0) { out.zeros++; return kids }
    kids.sort((a, b) => b.value - a.value)
    if (kids.length > maxParts) {
      const rest = kids.slice(maxParts - 1)
      const sum = rest.reduce((s, k) => s + k.value, 0)
      // parts may overlap (features used by the same respondents): a sum larger than the
      // whole is not a part of it, so the tail is left out rather than grouped
      if (own !== null && sum > own) {
        out.hiddenParts += rest.length
        kids = kids.slice(0, maxParts - 1)
      } else {
        out.grouped += rest.length
        kids = [...kids.slice(0, maxParts - 1), { id: [...path, 'Other'].join(' › '), name: `Other (${rest.length})`, value: sum, depth: 0, parent: null, children: [] }]
      }
    }
    const value = own ?? kids.reduce((s, k) => s + k.value, 0)
    if (value <= 0) {
      if (own === null && !(n.children ?? []).length) out.nulls++
      return kids
    }
    const node: NNode = { id: path.join(' › '), name: n.name, value, depth: 0, parent: null, children: kids }
    for (const k of kids) if (k.value > value) out.larger++
    return [node]
  }
  const roots = build(tree, [tree.name])
  // a root without a value of its own is a label over independent wholes, not a whole itself
  const ownless = roots.length === 1 && roots[0]!.id === tree.name && (tree.value ?? null) === null && roots[0]!.children.length > 0
  out.wholes = ownless ? roots[0]!.children : roots
  const reroot = (n: NNode, depth: number, parent: NNode | null) => {
    n.depth = depth
    n.parent = parent
    n.children.forEach((c) => reroot(c, depth + 1, n))
  }
  out.wholes.forEach((w) => reroot(w, 0, null))
  const chains = (n: NNode, prefix: NNode[]) => {
    const here = [...prefix, n]
    if (!n.children.length) out.panels.push(here)
    else n.children.forEach((c) => chains(c, here))
  }
  out.wholes.forEach((w) => chains(w, []))
  return out
}

const shareOf = (n: NNode) => (n.parent ? n.value / n.parent.value : 1)

/** Fill for level k of a chain of length len: the whole palest, the innermost part full strength. */
function levelFill(theme: Palette, hue: number, k: number, len: number): string {
  const base = theme.cat(hue)
  return len <= 1 ? base : theme.mix(base, theme.surface, 0.65 * (1 - k / (len - 1)))
}

export interface PanelBox { x: number; y: number; side: number; width: number; height: number }

/** Grid of panels: the arrangement that gives the largest squares, with room for a text column. */
export function panelGrid(n: number, width: number, height: number, gap = 16): { cols: number; rows: number; boxes: PanelBox[]; side: number } {
  let best = { cols: 1, rows: n, side: 0, pw: width, ph: height }
  for (let cols = 1; cols <= n; cols++) {
    const rows = Math.ceil(n / cols)
    const pw = (width - gap * (cols - 1)) / cols, ph = (height - gap * (rows - 1)) / rows
    // the text column gives way on a narrow chart, or many panels starve the squares
    const tw = Math.min(220, Math.max(Math.min(140, width * 0.3), pw * 0.42))
    const side = Math.min(pw - tw - 10, ph)
    if (side > best.side) best = { cols, rows, side, pw, ph }
  }
  const side = Math.max(4, best.side)
  const boxes = Array.from({ length: n }, (_, i) => ({ x: (i % best.cols) * (best.pw + gap), y: Math.floor(i / best.cols) * (best.ph + gap), side, width: best.pw, height: best.ph }))
  return { cols: best.cols, rows: best.rows, boxes, side }
}

export const nestedProportions = defineChart<NestedData, NestedOptions>({
  id: 'nested-proportions',
  name: 'Nested proportions',
  category: 'part-to-whole',
  intents: ['composition', 'hierarchy'],
  summary: 'A part drawn inside its whole: squares sharing a corner, area proportional to value.',
  input,
  options,
  shape: 'object',
  fields: ['label', 'value', 'parent', 'id', 'path'],
  numberFields: ['value'],
  adapt: (raw, o) => toTree(raw, o, nestedFromRows),
  isEmpty(data, o) {
    const m = nest(data, o.maxParts)
    if (m.panels.length) return false
    if (m.negatives) return `No positive values to show (${phrase.count(m.negatives, 'negative value')})`
    if (m.zeros) return 'All values are zero'
    return true
  },
  aspect: 0.55,
  minHeight: 200,
  draw(ctx) {
    const { theme, fmt, options: o } = ctx
    const m = nest(ctx.data, o.maxParts)
    const note = notes([[m.negatives, 'negative values omitted'], [m.zeros, 'zero values not drawn'], [m.nulls, 'missing values left out'], [m.merged, 'duplicate names summed'], [m.grouped, 'smaller parts grouped as Other'], [m.hiddenParts, 'smaller overlapping parts not shown'], [m.larger, 'parts larger than their whole']])
    const plot = { x: 0, y: 0, width: ctx.width, height: ctx.height }
    if (!m.panels.length) return { marks: [], note, plot }
    const grid = panelGrid(m.panels.length, ctx.width, ctx.height)
    let maxV = 0
    for (const p of m.panels) for (const n of p) maxV = Math.max(maxV, n.value)
    const sideOf = (v: number) => Math.max(1, grid.side * Math.sqrt(v / maxV))
    const fs = SIZES.tick
    const lh = fs * 1.3
    const sw = Math.min(10, fs)
    const marks: Mark[] = []
    const labels: Mark[] = []
    const drawn = new Set<string>()

    m.panels.forEach((chain, pi) => {
      const box = grid.boxes[pi]!
      const hue = m.panels.length > 1 ? pi : 0
      const len = chain.length
      // the text column: one entry per level; when they do not all fit, the wholes go first
      // (they repeat in the other panels) and the part this panel is about stays
      const twoLines = len * (lh * 2 + 6) <= box.height
      const entryH = twoLines ? lh * 2 + 6 : lh + 4
      const fit = Math.max(0, Math.min(len, Math.floor(box.height / entryH)))
      const shown = chain.slice(len - fit).map((n, i) => ({ n, k: len - fit + i }))
      const subOf = (n: NNode) => (n.parent ? `${fmt.value(n.value)} · ${fmt.pct(shareOf(n))} of ${n.parent.name}` : fmt.value(n.value))
      const lineOf = (n: NNode) => `${n.name} ${fmt.value(n.value)}`
      const textNeed = Math.min(220, Math.max(0, ...shown.map(({ n }) => sw + 6 + (twoLines ? Math.max(ctx.measure(n.name, fs, 600), ctx.measure(subOf(n), fs)) : ctx.measure(lineOf(n), fs)))))
      const wholeSide = sideOf(Math.max(...chain.map((n) => n.value)))
      const left = box.x + Math.max(0, (box.width - (wholeSide + 12 + textNeed)) / 2)
      const bottom = box.y + box.height - (box.height - grid.side) / 2
      // largest first so a part is drawn on top of its whole
      const order = chain.map((n, k) => ({ n, k })).sort((a, b) => b.n.value - a.n.value)
      for (const { n, k } of order) {
        const s = sideOf(n.value)
        const fill = levelFill(theme, hue, k, len)
        const primary = !drawn.has(n.id)
        drawn.add(n.id)
        marks.push({
          type: 'rect', role: primary ? 'square' : 'context', key: primary ? n.id : `ctx:${pi}:${n.id}`,
          x: left, y: bottom - s, width: s, height: s, fill,
          stroke: k === 0 && len > 1 ? theme.cat(hue) : theme.surface, strokeWidth: k === 0 && len > 1 ? 1 : 1.5,
          ...(primary
            ? {
                datum: { name: n.name, path: n.id.split(' › '), value: n.value, whole: n.parent?.name ?? null, shareOfWhole: shareOf(n) },
                label: n.parent ? `${n.name}: ${fmt.value(n.value)}, ${fmt.pct(shareOf(n))} of ${n.parent.name}` : `${n.name}: ${fmt.value(n.value)}`,
                tooltip: { title: n.name, rows: n.parent ? [['Value', fmt.value(n.value)], [`Share of ${n.parent.name}`, fmt.pct(shareOf(n))]] : [['Value', fmt.value(n.value)]], color: fill },
              }
            : { inert: true }),
        })
      }
      const tx = left + wholeSide + 12
      const nameW = box.x + box.width - tx - sw - 6
      if (nameW < 30 || !shown.length) return
      // level with the top of each square, pushed apart, then back up if they run off the bottom
      const ys: number[] = []
      shown.forEach(({ n }, i) => ys.push(Math.max(bottom - sideOf(n.value), i ? ys[i - 1]! + entryH : box.y)))
      const overflow = ys[ys.length - 1]! + entryH - (box.y + box.height)
      if (overflow > 0) for (let i = ys.length - 1; i >= 0; i--) ys[i] = Math.max(box.y + i * entryH, ys[i]! - overflow)
      shown.forEach(({ n, k }, i) => {
        const y = ys[i]!
        labels.push({ type: 'rect', layer: 'labels', key: `sw:${pi}:${n.id}`, x: tx, y: y + (lh - sw) / 2, width: sw, height: sw, rx: 2, fill: levelFill(theme, hue, k, len), stroke: theme.cat(hue), strokeWidth: 0.75, inert: true })
        const full = twoLines ? n.name : lineOf(n)
        const head = twoLines ? truncateBold(n.name, nameW, fs, ctx, 600) : ctx.truncate(full, nameW, fs)
        if (head && head !== '…') labels.push({ type: 'text', layer: 'labels', key: `n:${pi}:${n.id}`, x: tx + sw + 6, y: y + lh / 2, text: head, full: head !== full ? full : undefined, baseline: 'central', fill: theme.ink, size: fs, weight: twoLines ? 600 : 400, inert: head === full })
        if (!twoLines) return
        const sub = subOf(n)
        const t = ctx.truncate(sub, nameW, fs)
        if (t && t !== '…') labels.push({ type: 'text', layer: 'labels', key: `s:${pi}:${n.id}`, x: tx + sw + 6, y: y + lh * 1.5, text: t, full: t !== sub ? sub : undefined, baseline: 'central', fill: theme.muted, size: fs, inert: t === sub })
      })
    })
    return { marks: [...marks, ...labels], note, plot }
  },
  describe({ data, options: o, fmt, name }) {
    const m = nest(data, o.maxParts)
    if (!m.panels.length) return `${name}, no values`
    if (m.panels.length === 1) {
      const chain = m.panels[0]!
      return `${name}, ${chain.map((n, k) => (k ? `${n.name} ${fmt.value(n.value)} (${fmt.pct(shareOf(n))})` : `${n.name} ${fmt.value(n.value)}`)).join(' → ')}`
    }
    const parts = m.wholes.map((w) => {
      const kids = w.children.map((c) => `${c.name} ${fmt.pct(shareOf(c))}`)
      return `of ${w.name} (${fmt.value(w.value)}): ${phrase.listPhrase(kids)}`
    })
    return `${name}, ${phrase.count(m.panels.length, 'panel')}, ${parts.join('; ')}`
  },
  table(data, o) {
    const m = nest(data, o.maxParts)
    const rows: Array<Array<string | number | null>> = []
    const walk = (n: NNode) => {
      rows.push([n.id, n.value, n.parent?.name ?? null, n.parent ? shareOf(n) : null])
      n.children.forEach(walk)
    }
    m.wholes.forEach(walk)
    return { columns: ['Path', 'Value', 'Whole', 'Share of whole'], rows }
  },
})

export default nestedProportions
