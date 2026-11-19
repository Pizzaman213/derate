// Shared by the hierarchy charts (tree, radial-tree, dendrogram, circle-packing,
// org-chart): one tree input accepted in three forms, stable keys, depth collapsing,
// the hidden table, node tooltips and a collision-checking label placer.

import { z } from 'zod'
import { hierarchy, type HierarchyNode } from 'd3-hierarchy'
import { AdapterError, mapFields, pathsToTree, stratifyRows, toNumber, toRows, treeFromNested, type Row, type TreeNode } from '../../adapters'
import { columnsOf, isRecord } from '../../adapters/util'
import type { Formatters, Mark, Palette, TableModel, TooltipContent } from '../../core'

/** One node of the canonical tree. */
export interface TreeDatum {
  name: string
  value?: number
  title?: string
  height?: number
  children?: TreeDatum[]
  [k: string]: unknown
}

const nodeSchema: z.ZodType<TreeDatum, z.ZodTypeDef, unknown> = z.lazy(() =>
  z
    .object({
      name: z.string({ invalid_type_error: 'text' }),
      value: z.number({ invalid_type_error: 'number' }).finite().optional(),
      title: z.string({ invalid_type_error: 'text' }).optional(),
      height: z.number({ invalid_type_error: 'number' }).finite().optional(),
      children: z.array(nodeSchema).optional(),
    })
    .passthrough(),
) as unknown as z.ZodType<TreeDatum, z.ZodTypeDef, unknown>

/** The canonical input: a tree, or null when there were no rows. */
export const treeInput = nodeSchema
  .nullable()
  .describe('a nested { name, value?, children? } object; or rows of { id, parent, name?, value? }; or rows of { path, value } (CSV and column arrays too)')

/** Options every hierarchy chart reads in `adapt`. */
export const separatorOption = z.string().min(1).default('/').describe("path separator for { path, value } rows")

const showRaw = (v: unknown): string => (typeof v === 'string' ? `'${v.length > 40 ? v.slice(0, 40) + '…' : v}'` : Array.isArray(v) ? `a list of ${v.length}` : typeof v === 'object' ? 'an object' : String(v))

function checkNumbers(rows: readonly Row[], field: string): void {
  rows.forEach((r, i) => {
    const v = r[field]
    if (v === null || v === undefined || v === '') return
    if (toNumber(v) === null) throw new AdapterError(`field '${field}' must be a number, got ${showRaw(v)} at row ${i + 1}`)
  })
}

function textOf(v: unknown): string | undefined {
  if (v === null || v === undefined || v === '') return undefined
  return String(v)
}

/** Strings/numbers in title, numeric strings in height: the nested form gets the same coercion as rows. */
function coerceNested(n: TreeNode): TreeNode {
  const out: TreeNode = { ...n }
  if (out.title !== undefined) {
    const t = typeof out.title === 'number' ? String(out.title) : out.title
    if (t === null || t === '') delete out.title
    else out.title = t
  }
  if (out.height !== undefined) {
    if (out.height === null || out.height === '') delete out.height
    else if (typeof out.height === 'string' && toNumber(out.height) !== null) out.height = toNumber(out.height)
  }
  if (out.children) out.children = out.children.map(coerceNested)
  return out
}

/**
 * Raw input -> canonical tree. Accepts a nested object, id/parent rows (stratified;
 * cycles and missing parents are errors), or path rows; rows may be CSV text or column
 * arrays and go through `options.fields` first. No rows -> null (the empty state).
 */
export function adaptTree(raw: unknown, o: { fields?: Record<string, string>; separator?: string }): TreeDatum | null {
  if (raw === null || raw === undefined) return null
  if (isRecord(raw) && !Object.values(raw).every(Array.isArray)) return coerceNested(treeFromNested(raw)) as TreeDatum
  if (isRecord(raw) && Object.keys(raw).length === 0) return null
  const parsed = toRows(raw)
  if (parsed === null) throw new AdapterError(`the data must be a nested { name, children } object, or rows of { id, parent } or { path, value } (CSV text and column arrays work too), got ${showRaw(raw)}`)
  const rows = o.fields && Object.keys(o.fields).length ? mapFields(parsed, o.fields) : parsed
  if (rows.length === 0) return null
  const cols = columnsOf(rows)
  checkNumbers(rows, 'value')
  if (cols.includes('path')) return pathsToTree(rows, { path: 'path', value: 'value', separator: o.separator || '/' }) as TreeDatum
  if (!cols.includes('id')) throw new AdapterError(`rows need an 'id' column (with 'parent') or a 'path' column; got columns ${cols.map((c) => `'${c}'`).join(', ')} -- map yours with options.fields, e.g. { id: 'employee', parent: 'manager' }`)
  checkNumbers(rows, 'height')
  const tree = stratifyRows(rows, { id: 'id', parent: 'parent', name: cols.includes('name') ? 'name' : undefined, value: cols.includes('value') ? 'value' : undefined })
  const byId = new Map<string, Row>()
  for (const r of rows) byId.set(String(r.id), r)
  const extras = (n: TreeNode): void => {
    const r = typeof n.id === 'string' ? byId.get(n.id) : undefined
    if (r) {
      if (r.name === null || r.name === undefined || r.name === '') n.name = String(r.id)
      const title = textOf(r.title)
      if (title !== undefined) n.title = title
      const h = toNumber(r.height)
      if (h !== null) n.height = h
    }
    for (const c of n.children ?? []) extras(c)
  }
  extras(tree)
  return tree as TreeDatum
}

export type TNode = HierarchyNode<TreeDatum>

export interface BuiltTree {
  root: TNode
  /** Sibling names that repeat (kept as separate nodes, keys suffixed). */
  duplicates: number
  /** Nodes hidden under collapsed nodes (maxDepth). */
  collapsed: number
  /** Hidden descendants per collapsed node. */
  hiddenBelow: Map<TNode, number>
  /** Index of the top-level branch each node belongs to, in data order (root: -1). */
  branch: Map<TNode, number>
  /** The root's children in data order, before `exclude`. */
  top: TNode[]
}

/**
 * d3 hierarchy with a stable path key in `node.id` (duplicate sibling names get `~2`, `~3`),
 * top-level branches optionally excluded (legend toggles), summed (`sum` sees each datum),
 * sorted, and collapsed below `maxDepth`.
 */
export function buildTree(
  data: TreeDatum,
  opts: { maxDepth?: number; sum?: (d: TreeDatum) => number; sort?: (a: TNode, b: TNode) => number; exclude?: (top: TNode) => boolean } = {},
): BuiltTree {
  const root = hierarchy<TreeDatum>(data, (d) => (d.children && d.children.length ? d.children : null))
  let duplicates = 0
  root.each((n) => {
    if (!n.parent) Object.assign(n, { id: n.data.name })
    const kids = n.children
    if (!kids) return
    const seen = new Map<string, number>()
    for (const c of kids) {
      const k = (seen.get(c.data.name) ?? 0) + 1
      seen.set(c.data.name, k)
      if (k > 1 && c.data.name) duplicates++
      Object.assign(c, { id: `${n.id}/${c.data.name.replace(/[/~]/g, (ch) => (ch === '/' ? '%2F' : '%7E'))}${k > 1 ? `~${k}` : ''}` })
    }
  })
  const branch = new Map<TNode, number>()
  const top = root.children ? [...root.children] : []
  top.forEach((c, i) => c.each((d) => branch.set(d, i)))
  branch.set(root, -1)
  if (opts.exclude && root.children) {
    const keep = root.children.filter((c) => !opts.exclude!(c))
    root.children = keep.length ? keep : undefined
  }
  if (opts.sum) root.sum(opts.sum)
  if (opts.sort) root.sort(opts.sort)
  const hiddenBelow = new Map<TNode, number>()
  let collapsed = 0
  if (opts.maxDepth !== undefined) {
    const limit = opts.maxDepth
    root.each((n) => {
      if (n.depth === limit && n.children) {
        const hidden = n.descendants().length - 1
        hiddenBelow.set(n, hidden)
        collapsed += hidden
        n.children = undefined
      }
    })
  }
  return { root, duplicates, collapsed, hiddenBelow, branch, top }
}

/** A node's name; unnamed inner nodes (dendrogram merges) read as "Group of N". */
export function displayName(n: TNode): string {
  if (n.data.name) return n.data.name
  return n.children ? `Group of ${n.leaves().length}` : 'Unnamed'
}

/** "Root › Branch › Leaf" (the middle is elided for deep nodes so tooltips stay short). */
export function pathOf(n: TNode): string {
  const names = n.ancestors().reverse().map(displayName)
  return names.length > 4 ? `${names[0]} › … › ${names.slice(-2).join(' › ')}` : names.join(' › ')
}

/** Announced sentence for a node. */
export function nodeLabel(n: TNode, fmt: Formatters, hidden = 0, value: number | null | undefined = n.data.value): string {
  let s = displayName(n)
  if (n.data.title) s += ` (${n.data.title})`
  s += n.parent ? `, under ${displayName(n.parent)}` : ', root'
  if (typeof value === 'number') s += `: ${fmt.value(value)}`
  const kids = n.children?.length ?? 0
  if (kids) s += `, ${kids} ${kids === 1 ? 'child' : 'children'}`
  if (hidden) s += `, ${hidden} collapsed`
  return s
}

/** Tooltip rows for a node. */
export function nodeTooltip(n: TNode, fmt: Formatters, color: string, hidden = 0, value: number | null | undefined = n.data.value, extra: Array<[string, string]> = []): TooltipContent {
  const rows: Array<[string, string]> = []
  if (n.data.title) rows.push(['Title', n.data.title])
  if (n.parent) rows.push(['Path', pathOf(n)])
  if (typeof value === 'number') rows.push(['Value', fmt.value(value)])
  const kids = n.children?.length ?? 0
  if (kids) rows.push(['Children', fmt.integer(kids)])
  if (hidden) rows.push(['Collapsed', `${fmt.integer(hidden)} below`])
  rows.push(...extra)
  return { title: displayName(n), rows, color }
}

/** Pre-order table of the whole (uncollapsed) tree. */
export function treeTable(data: TreeDatum | null, extra: { title?: boolean; height?: boolean } = {}): TableModel {
  if (!data) return { columns: ['Path', 'Name', 'Depth', 'Value'], rows: [] }
  const { root } = buildTree(data)
  const nodes = root.descendants()
  const title = extra.title ?? nodes.some((n) => n.data.title !== undefined)
  const height = extra.height ?? nodes.some((n) => n.data.height !== undefined)
  const columns = ['Path', 'Name', ...(title ? ['Title'] : []), 'Depth', 'Value', ...(height ? ['Height'] : [])]
  const rows = nodes.map((n) => [
    n.ancestors().reverse().map((a) => a.data.name).join(' / '),
    n.data.name,
    ...(title ? [n.data.title ?? null] : []),
    n.depth,
    n.data.value ?? null,
    ...(height ? [n.data.height ?? null] : []),
  ])
  return { columns, rows }
}

/** Counts for summaries. */
export function treeFacts(data: TreeDatum): { nodes: number; leaves: number; levels: number; root: string; widest: { depth: number; count: number }; top: string[] } {
  const { root } = buildTree(data)
  const perDepth: number[] = []
  let leaves = 0
  root.each((n) => {
    perDepth[n.depth] = (perDepth[n.depth] ?? 0) + 1
    if (!n.children) leaves++
  })
  let wd = 0
  perDepth.forEach((c, d) => {
    if (c > (perDepth[wd] ?? 0)) wd = d
  })
  return { nodes: root.descendants().length, leaves, levels: perDepth.length, root: root.data.name, widest: { depth: wd, count: perDepth[wd] ?? 0 }, top: (root.children ?? []).map((c) => c.data.name) }
}

/** Paint for a node: the first categorical colour, or its top-level branch's. */
export function branchColor(theme: Palette, b: BuiltTree, n: TNode, by: 'single' | 'branch'): string {
  if (by === 'single') return theme.cat(0)
  const i = b.branch.get(n) ?? -1
  return i < 0 ? theme.ink : theme.cat(i)
}

export interface Box { x0: number; y0: number; x1: number; y1: number }

/** Greedy label placement: a box is accepted only when it overlaps no accepted box. */
export class Placer {
  private cells = new Map<string, Box[]>()
  constructor(private readonly cell = 48) {}
  private keys(b: Box): string[] {
    const out: string[] = []
    const c = this.cell
    for (let i = Math.floor(b.x0 / c); i <= Math.floor(b.x1 / c); i++) for (let j = Math.floor(b.y0 / c); j <= Math.floor(b.y1 / c); j++) out.push(`${i},${j}`)
    return out
  }
  fits(b: Box): boolean {
    for (const k of this.keys(b)) for (const o of this.cells.get(k) ?? []) if (b.x0 < o.x1 && b.x1 > o.x0 && b.y0 < o.y1 && b.y1 > o.y0) return false
    return true
  }
  add(b: Box): void {
    for (const k of this.keys(b)) {
      const list = this.cells.get(k)
      if (list) list.push(b)
      else this.cells.set(k, [b])
    }
  }
  /** Add when free; returns whether it was placed. */
  place(b: Box): boolean {
    if (!this.fits(b)) return false
    this.add(b)
    return true
  }
}

/** A text mark with `full` set when it was shortened. */
export function fittedText(base: Omit<Extract<Mark, { type: 'text' }>, 'text' | 'full'>, full: string, shown: string): Mark {
  return shown === full ? { ...base, text: shown } : { ...base, text: shown, full }
}

/** Common malformed-input cases every hierarchy chart rejects (for tests). */
export const MALFORMED = [
  { name: 'a cycle', data: [{ id: 'a', parent: 'b' }, { id: 'b', parent: 'a' }], message: /cycle 'a' -> 'b' -> 'a'|cycle 'b' -> 'a' -> 'b'/ },
  { name: 'a missing parent', data: [{ id: 'ceo', parent: null }, { id: 'cto', parent: 'ceo' }, { id: 'dev', parent: 'cfo' }], message: /row 3: parent 'cfo' not found/ },
  { name: 'a duplicate id', data: 'id,parent\na,\nb,a\nb,a', message: /row 3: duplicate id 'b'/ },
  { name: 'a text value in rows', data: [{ id: 'a', parent: null, value: 1 }, { id: 'b', parent: 'a', value: 'abc' }], message: /field 'value' must be a number, got 'abc' at row 2/ },
  { name: 'a text value in a nested tree', data: { name: 'r', children: [{ name: 'a', value: 'abc' }] }, message: /root\.children\[0\]\.value must be a finite number/ },
  { name: 'rows without an id or path column', data: [{ who: 'a', boss: null }], message: /rows need an 'id' column/ },
  { name: 'a scalar', data: 42, message: /must be a nested \{ name, children \} object/ },
  { name: 'a mapping to a missing column', data: [{ employee: 'a', manager: null }], options: { fields: { id: 'employee', parent: 'mgr' } }, message: /column 'mgr' not found/ },
]
