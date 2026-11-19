// Shared by treemap, sunburst, icicle and nested-proportions: one canonical tree, every
// accepted input form turned into it (nested object, {path, value} rows, {id, parent,
// value} rows, {label, value, parent?} rows -- as arrays, CSV text or column arrays,
// through a field mapping), and one cleaning pass that drops what cannot have an area
// (negative, zero, missing) and counts it for the note.

import { z } from 'zod'
import { AdapterError, isRecord, mapFields, pathsToTree, stratifyRows, toRows, treeFromNested, type Row, type TreeNode } from '../../adapters'
import { phrase, type Formatters, type LegendSpec, type Mark, type Palette, type TableModel } from '../../core'
import { notes } from '../../core/helpers'

/** The canonical tree every hierarchy chart validates. */
export interface TreeInput {
  name: string
  value?: number | null
  children?: TreeInput[]
  /** Rows merged by the adapter (duplicate labels / paths), carried for the note. */
  merged?: number
  [k: string]: unknown
}

export const treeInput: z.ZodType<TreeInput, z.ZodTypeDef, unknown> = z.lazy(() =>
  z.object({
    name: z.string(),
    value: z.number().finite().nullable().optional(),
    children: z.array(treeInput).optional(),
    merged: z.number().int().nonnegative().optional(),
  }).passthrough(),
).describe('a tree { name, value?, children? }; also rows of { path, value }, { id, parent, value } or { label, value, parent? }')

/** Options every hierarchy chart shares. */
export const separatorOption = z.string().min(1).default('/').describe("separator inside 'path' values")

const show = (v: unknown): string => (typeof v === 'string' ? `'${v.length > 40 ? v.slice(0, 40) + '…' : v}'` : typeof v === 'object' && v !== null ? 'an object' : String(v))

/** A cell as a number: numbers, numeric strings ("1,234.5"); blank -> null; anything else throws. */
export function cellNumber(v: unknown, field: string, row: number): number | null {
  if (v === null || v === undefined || v === '') return null
  if (typeof v === 'number') {
    if (Number.isNaN(v)) return null
    if (Number.isFinite(v)) return v
  }
  if (typeof v === 'string') {
    const t = v.trim()
    if (t === '') return null
    const n = Number(t.replace(/(?<=\d),(?=\d{3}\b)/g, ''))
    if (Number.isFinite(n)) return n
  }
  throw new AdapterError(`field '${field}' must be a number, got ${show(v)} at row ${row}`)
}

export const cellText = (v: unknown): string | null => (v === null || v === undefined || v === '' ? null : String(v))

function columnsOf(rows: readonly Row[]): Set<string> {
  const s = new Set<string>()
  for (const r of rows) for (const k of Object.keys(r)) s.add(k)
  return s
}

/** Rows of {path, value}, {id, parent, value} or {label|name, value, parent?} -> a tree. */
export function treeFromRows(rows: readonly Row[], separator = '/'): TreeInput {
  if (rows.length === 0) return { name: 'All', children: [] }
  const cols = columnsOf(rows)
  if (cols.has('path')) {
    const seen = new Set<string>()
    let merged = 0
    const clean = rows.map((r, i) => {
      const value = cellNumber(r.value, 'value', i + 1)
      const key = String(r.path ?? '').split(separator).filter(Boolean).join('\u0000')
      if (seen.has(key)) merged++
      seen.add(key)
      return { path: r.path, value }
    })
    const tree = pathsToTree(clean, { path: 'path', value: 'value', separator }) as TreeInput
    return merged ? { ...tree, merged } : tree
  }
  if (cols.has('id')) {
    const clean = rows.map((r, i) => ({ ...r, value: cellNumber(r.value, 'value', i + 1) }))
    return stratifyRows(clean, { id: 'id', parent: 'parent', value: 'value', name: cols.has('name') ? 'name' : undefined }) as TreeInput
  }
  const labelCol = cols.has('label') ? 'label' : cols.has('name') ? 'name' : null
  if (labelCol === null) {
    throw new AdapterError(`rows need a 'path' column ('a/b/c'), 'id' and 'parent' columns, or a 'label' column (columns: ${[...cols].join(', ')})`)
  }
  // one node per label; a repeated label sums into the first one
  const byLabel = new Map<string, { label: string; parent: string | null; value: number | null }>()
  let merged = 0
  rows.forEach((r, i) => {
    const label = cellText(r[labelCol]) ?? '(blank)'
    const value = cellNumber(r.value, 'value', i + 1)
    const parent = cellText(r.parent)
    const hit = byLabel.get(label)
    if (!hit) byLabel.set(label, { label, parent, value })
    else {
      merged++
      hit.value = hit.value === null ? value : value === null ? hit.value : hit.value + value
      if (hit.parent === null) hit.parent = parent
    }
  })
  const list = [...byLabel.values()]
  let tree: TreeInput
  if (list.some((r) => r.parent !== null)) {
    tree = stratifyRows(list, { id: 'label', parent: 'parent', value: 'value' }) as TreeInput
  } else {
    tree = { name: 'All', children: list.map((r) => (r.value === null ? { name: r.label } : { name: r.label, value: r.value })) }
  }
  return merged ? { ...tree, merged: (tree.merged ?? 0) + merged } : tree
}

/** Any accepted input -> the canonical tree (the charts' `adapt`); `fromRows` turns mapped rows into the tree. */
export function toTree(raw: unknown, options: { fields?: Record<string, string>; separator?: string }, fromRows: (rows: readonly Row[], separator: string) => TreeInput = treeFromRows): TreeInput {
  if (raw === null || raw === undefined) return { name: 'All', children: [] }
  const rows = typeof raw === 'string' || Array.isArray(raw) || isRecord(raw) ? toRows(raw) : null
  if (rows !== null) {
    const mapped = options.fields && Object.keys(options.fields).length ? mapFields(rows, options.fields) : rows
    return fromRows(mapped, options.separator ?? '/')
  }
  if (isRecord(raw)) return treeFromNested(raw as TreeNode) as TreeInput
  throw new AdapterError(`the data must be a tree { name, value?, children? }, rows of { path, value }, { id, parent, value } or { label, value, parent? }, or CSV text of those, got ${Array.isArray(raw) ? 'a list of values' : show(raw)}`)
}

/** A node after cleaning: values summed bottom-up, children sorted largest first. */
export interface PNode {
  /** Stable key: the names from the root joined with ' › '. */
  id: string
  name: string
  value: number
  depth: number
  /** Names from the first level below the root down to this node (empty for the root). */
  path: string[]
  children: PNode[]
  /** Index of the first-level branch this node belongs to (colour), -1 for the root. */
  branch: number
}

export interface Cleaned {
  root: PNode | null
  negatives: number
  zeros: number
  nulls: number
  merged: number
  /** First-level branch names, in colour order (before hiding). */
  branches: string[]
  /** Leaves kept. */
  leaves: number
  maxDepth: number
}

/** Siblings sharing a name become one node (values summed, children concatenated); counted in `count.merged`. */
export function mergeSiblings(n: TreeInput, count: { merged: number }): TreeInput {
  if (!n.children || n.children.length === 0) return n
  const by = new Map<string, TreeInput>()
  for (const c of n.children) {
    const hit = by.get(c.name)
    if (!hit) {
      by.set(c.name, { ...c })
      continue
    }
    count.merged++
    const a = hit.value ?? null, b = c.value ?? null
    hit.value = a === null ? b : b === null ? a : a + b
    if (c.children?.length) hit.children = [...(hit.children ?? []), ...c.children]
  }
  return { ...n, children: [...by.values()].map((c) => mergeSiblings(c, count)) }
}

const byValue = (a: PNode, b: PNode) => b.value - a.value || (a.name < b.name ? -1 : a.name > b.name ? 1 : 0)

/** Drop what cannot have an area, sum bottom-up, sort largest first; hidden first-level branches are left out. */
export function cleanTree(input: TreeInput, hidden: ReadonlySet<string> = new Set()): Cleaned {
  const count = { merged: input.merged ?? 0 }
  const t = mergeSiblings(input, count)
  const out: Cleaned = { root: null, negatives: 0, zeros: 0, nulls: 0, merged: count.merged, branches: [], leaves: 0, maxDepth: 0 }
  const build = (n: TreeInput, path: string[], depth: number): PNode | null => {
    const id = [t.name, ...path].join(' › ')
    if (n.children && n.children.length > 0) {
      const kids: PNode[] = []
      for (const c of n.children) {
        const k = build(c, [...path, c.name], depth + 1)
        if (k) kids.push(k)
      }
      if (!kids.length) return null
      kids.sort(byValue)
      return { id, name: n.name, value: kids.reduce((s, k) => s + k.value, 0), depth, path, children: kids, branch: -1 }
    }
    const v = n.value ?? null
    if (v === null) { out.nulls++; return null }
    if (v < 0) { out.negatives++; return null }
    if (v === 0) { out.zeros++; return null }
    out.leaves++
    out.maxDepth = Math.max(out.maxDepth, depth)
    return { id, name: n.name, value: v, depth, path, children: [], branch: -1 }
  }
  const root = build(t, [], 0)
  if (!root) return out
  out.branches = root.children.map((c) => c.name)
  const visible = hidden.size ? root.children.filter((c) => !hidden.has(c.name)) : root.children
  const stamp = (n: PNode, b: number) => {
    n.branch = b
    for (const c of n.children) stamp(c, b)
  }
  root.children.forEach((c, i) => stamp(c, i))
  if (visible.length !== root.children.length) {
    root.children = visible
    root.value = visible.reduce((s, k) => s + k.value, 0)
    out.leaves = 0
    out.maxDepth = 0
    const walk = (n: PNode) => {
      if (!n.children.length) { out.leaves++; out.maxDepth = Math.max(out.maxDepth, n.depth) }
      n.children.forEach(walk)
    }
    visible.forEach(walk)
  }
  out.root = root
  return out
}

/** Pre-order list of nodes. */
export function nodesOf(root: PNode | null): PNode[] {
  const out: PNode[] = []
  const visit = (n: PNode) => {
    out.push(n)
    n.children.forEach(visit)
  }
  if (root) visit(root)
  return out
}

/** The isEmpty answer for a cleaned tree. */
export function emptyMessage(c: Cleaned): boolean | string {
  if (c.root && c.root.value > 0) return false
  if (c.negatives > 0) return `No positive values to show (${phrase.count(c.negatives, 'negative value')})`
  if (c.zeros > 0) return 'All values are zero'
  return true
}

/** The visible note: what was left out and what was merged. */
export function treeNote(c: Cleaned, extra: Array<[number, string]> = []): string | undefined {
  return notes([[c.negatives, 'negative values omitted'], [c.zeros, 'zero values not drawn'], [c.nulls, 'missing values left out'], [c.merged, 'duplicate names summed'], ...extra])
}

/** Fill for a node: its branch colour, lighter with depth (an inner root: the neutral track). */
export function nodeFill(theme: Palette, n: PNode): string {
  if (n.branch < 0) return n.children.length ? theme.track : theme.cat(0)
  const base = theme.cat(n.branch)
  const t = Math.min(0.3, 0.12 * Math.max(0, n.depth - 1))
  return t > 0 ? theme.mix(base, theme.surface, t) : base
}

/** "Europe › France › Paris" (the root's own name for the root). */
export const pathText = (n: PNode, root: PNode): string => (n.path.length ? n.path.join(' › ') : root.name)

/** The datum handed to onClick / onHover. */
export interface NodeDatum { id: string; name: string; path: string[]; depth: number; value: number; share: number }
export const datumOf = (n: PNode, root: PNode): NodeDatum => ({ id: n.id, name: n.name, path: n.path, depth: n.depth, value: n.value, share: root.value > 0 ? n.value / root.value : 0 })

/** Label, tooltip and series for a node's data mark. */
export function nodeMarkProps(n: PNode, root: PNode, fmt: Formatters, color: string) {
  const share = root.value > 0 ? n.value / root.value : 0
  const where = pathText(n, root)
  const parent = n.depth > 1 ? findParent(root, n) : null
  const rows: Array<[string, string]> = [['Value', fmt.value(n.value)], ['Share of total', fmt.pct(share)]]
  if (parent) rows.push([`Share of ${parent.name}`, fmt.pct(n.value / parent.value)])
  if (n.children.length) rows.push(['Items', String(countLeaves(n))])
  return {
    datum: datumOf(n, root),
    label: n.depth === 0
      ? `${root.name}: ${fmt.value(n.value)} in total`
      : `${where}: ${fmt.value(n.value)} (${fmt.pct(share)} of total${parent ? `; ${fmt.pct(n.value / parent.value)} of ${parent.name}` : ''})`,
    tooltip: { title: where, rows, color },
    series: n.branch >= 0 ? root.children.find((c) => c.branch === n.branch)?.name : undefined,
  }
}

function findParent(root: PNode, n: PNode): PNode | null {
  let cur: PNode = root
  for (let i = 0; i < n.path.length - 1; i++) {
    const next = cur.children.find((c) => c.name === n.path[i])
    if (!next) return null
    cur = next
  }
  return cur
}

export function countLeaves(n: PNode): number {
  return n.children.length ? n.children.reduce((s, c) => s + countLeaves(c), 0) : 1
}

/** `truncate` for bold text: ctx.truncate measures at weight 400, which is narrower than 600-700. */
export function truncateBold(text: string, width: number, size: number, ctx: { measure: (t: string, s?: number, w?: number | string) => number; truncate: (t: string, w: number, s?: number) => string }, weight = 700): string {
  let w = width
  let t = ctx.truncate(text, w, size)
  while (t && ctx.measure(t, size, weight) > width && w > 0) {
    w -= Math.max(2, size / 2)
    t = ctx.truncate(text, w, size)
  }
  return t
}

/** One legend item per first-level branch (2 to 12 of them). */
export function branchLegend(c: Cleaned, theme: Palette): LegendSpec | undefined {
  if (c.branches.length < 2 || c.branches.length > 12) return undefined
  return { items: c.branches.map((b, i) => ({ key: b, label: b, color: theme.cat(i), shape: 'square' as const, pattern: i })) }
}

/** "3 groups, 12 items, total 1,234, largest Engineering at 450 (36%)" */
export function describeTree(name: string, c: Cleaned, fmt: Formatters): string {
  const root = c.root
  if (!root) return `${name}, no values`
  const leaves = nodesOf(root).filter((n) => !n.children.length)
  const top = root.children
  const parts: string[] = [name]
  if (c.maxDepth > 1) parts.push(phrase.count(top.length, 'group'))
  parts.push(phrase.count(leaves.length, 'item'))
  parts.push(`total ${fmt.value(root.value)}`)
  const big = top[0]
  if (big && top.length > 1) parts.push(`largest ${big.name} at ${fmt.value(big.value)} (${fmt.pct(big.value / root.value)})`)
  return parts.join(', ')
}

/** The hidden table / CSV: every node kept, pre-order. */
export function treeTable(c: Cleaned): TableModel {
  const root = c.root
  if (!root) return { columns: ['Path', 'Level', 'Value', 'Share of total'], rows: [] }
  return {
    columns: ['Path', 'Level', 'Value', 'Share of total'],
    rows: nodesOf(root).map((n) => [pathText(n, root), n.depth, n.value, root.value > 0 ? n.value / root.value : 0]),
  }
}

/** Text that fits a box: one or two lines (name, value) placed from the top-left, or nothing. */
export function boxLabels(opts: {
  key: string
  x: number; y: number; width: number; height: number
  name: string; value: string
  fill: string
  size: number
  measure: (t: string, s?: number, w?: number | string) => number
  truncate: (t: string, w: number, s?: number) => string
  weight?: number
  pad?: number
}): Mark[] {
  const pad = opts.pad ?? 5
  const lh = opts.size * 1.25
  const avail = opts.width - pad * 2
  if (avail < opts.size * 1.6 || opts.height < lh + pad) return []
  const text = truncateBold(opts.name, avail, opts.size, opts, opts.weight ?? 400)
  if (!text || text === '…' || [...text].length < 3 && text.endsWith('…')) return []
  const marks: Mark[] = [{ type: 'text', layer: 'labels', key: `l:${opts.key}`, x: opts.x + pad, y: opts.y + pad, text, full: text !== opts.name ? opts.name : undefined, baseline: 'hanging', anchor: 'start', fill: opts.fill, size: opts.size, weight: opts.weight, inert: text === opts.name }]
  if (opts.value && opts.height >= lh * 2 + pad && opts.measure(opts.value, opts.size) <= avail) {
    marks.push({ type: 'text', layer: 'labels', key: `v:${opts.key}`, x: opts.x + pad, y: opts.y + pad + lh, text: opts.value, baseline: 'hanging', anchor: 'start', fill: opts.fill, size: opts.size, opacity: 0.85, inert: true })
  }
  return marks
}
