// Shared by the flow/network charts (sankey, chord, arc-diagram, force-network): the
// graph input schema, the adapter that turns link rows / CSV / an adjacency matrix into
// it, normalisation (summed duplicates, dropped nulls/negatives, nodes named only by a
// link), cycle finding for a readable error, node colours and greedy label placement.

import { z } from 'zod'
import { fields } from '../../core'
import type { BaseOptions, LegendSpec, Palette } from '../../core'
import { describeIssue } from '../../core/validate'
import { AdapterError, graphFromMatrix, isRecord, mapFields, toGraph, toNumber, toRows } from '../../adapters'

/** A node or link id: text, numbers stringified, never empty. */
export const nodeId = (description: string) =>
  z.preprocess((v) => (typeof v === 'number' || typeof v === 'boolean' ? String(v) : v), z.string({ invalid_type_error: 'text', required_error: 'text' }).min(1)).describe(description)

export const graphNode = z.object({
  id: nodeId('node id; links refer to it'),
  label: fields.optLabel('display name (default: the id)'),
  group: fields.optLabel('group: colour and legend'),
  value: fields.numOrNull('optional node size'),
}).passthrough()

export const graphLink = z.object({
  source: nodeId('id of the node the link starts at'),
  target: nodeId('id of the node the link ends at'),
  value: fields.numOrNull('link weight; omit everywhere for an unweighted graph'),
}).passthrough()

export const graphInput = z.object({
  nodes: z.array(graphNode).optional().describe('nodes; ids only named by a link are added'),
  links: z.array(graphLink).describe('links between node ids'),
}).describe('{ nodes?: [{ id, label?, group?, value? }], links: [{ source, target, value? }] }')

export type GraphInput = z.output<typeof graphInput>

const LINK_COLUMNS_HINT = "map yours with options.fields, e.g. { source: 'from', target: 'to', value: 'amount' }"

/** Link rows (array, CSV text, column arrays) -> { nodes, links } through toGraph. */
function rowsToGraph(rows: Record<string, unknown>[], mapping: Record<string, string> | undefined): unknown {
  const mapped = mapping && Object.keys(mapping).length ? mapFields(rows, mapping) : rows
  if (mapped.length === 0) return { nodes: [], links: [] }
  const cols = new Set(mapped.flatMap((r) => Object.keys(r)))
  for (const need of ['source', 'target'])
    if (!cols.has(need)) throw new AdapterError(`link rows need a '${need}' column (columns: ${[...cols].join(', ')}); ${LINK_COLUMNS_HINT}`)
  if (cols.has('value'))
    mapped.forEach((r, i) => {
      const v = r.value
      if (v !== null && v !== undefined && v !== '' && toNumber(v) === null) throw new AdapterError(`field 'value' must be a number, got '${String(v)}' at row ${i + 1}`)
    })
  const g = toGraph(mapped, { source: 'source', target: 'target', value: cols.has('value') ? 'value' : undefined })
  // without a value column toGraph counts repeats; a graph where nothing repeats is unweighted
  const unweighted = !cols.has('value') && g.links.every((l) => l.value === 1)
  return { nodes: g.nodes, links: g.links.map((l) => ({ ...l, value: unweighted ? null : l.value })) }
}

/**
 * Raw input -> the graph object shape (not yet validated). Accepts `{ nodes?, links }`,
 * link rows / CSV / column arrays (with options.fields), and `{ names, matrix }`.
 */
export function adaptGraph(raw: unknown, options: BaseOptions): unknown {
  if (raw === null || raw === undefined) return { links: [] }
  if (isRecord(raw) && 'links' in raw) return raw
  if (isRecord(raw) && Array.isArray(raw.names) && Array.isArray(raw.matrix)) {
    const names = raw.names.map((n) => String(n))
    const matrix = raw.matrix.map((row: unknown, i) => {
      if (!Array.isArray(row)) throw new AdapterError(`matrix row ${i + 1} must be a list of numbers`)
      return row.map((v: unknown, j) => {
        if (v === null || v === undefined || v === '') return 0
        const n = toNumber(v)
        if (n === null) throw new AdapterError(`matrix row ${i + 1}, column ${j + 1} must be a number, got '${String(v)}'`)
        return n
      })
    })
    return graphFromMatrix(names, matrix)
  }
  if (typeof raw === 'string' || Array.isArray(raw) || isRecord(raw)) {
    const rows = toRows(raw)
    if (rows) return rowsToGraph(rows, options.fields)
  }
  throw new AdapterError("the data must be { nodes?, links: [{ source, target, value? }] }, link rows, CSV text with source,target[,value] columns, or { names, matrix }")
}

/** Validate a graph-shaped value inside an adapter (chord's input is a matrix): errors read like core's. */
export function parseGraph(raw: unknown): GraphInput {
  const r = graphInput.safeParse(raw)
  if (r.success) return r.data
  throw new AdapterError(r.error.issues.map((i) => describeIssue(i, raw).message).join('; '))
}

export interface GNode {
  id: string
  label: string
  group: string | undefined
  value: number | null
  /** Position in the node list (input order). */
  index: number
  /** Sum of link values in / out (undirected graphs: both are the weighted degree). */
  inValue: number
  outValue: number
  /** Number of distinct links touching the node. */
  degree: number
}
export interface GLink {
  key: string
  source: string
  target: string
  value: number
}
export interface Net {
  nodes: GNode[]
  links: GLink[]
  byId: Map<string, GNode>
  /** False when no link carried a value (every link counts 1). */
  weighted: boolean
  groups: string[]
  dropped: { nulls: number; negatives: number; zeros: number; duplicates: number; selfLoops: number }
}

export interface NetOptions {
  /** Merge a->b and b->a into one undirected link. */
  undirected?: boolean
  /** Keep self loops (otherwise dropped and counted). */
  selfLoops?: boolean
  /** Keep zero-valued links (otherwise dropped and counted). */
  keepZero?: boolean
}

const netCache = new WeakMap<object, Map<string, Net>>()

/** Canonical graph -> nodes with labels/degree, summed links; what was left out is counted. */
export function normalizeNet(data: GraphInput, o: NetOptions = {}): Net {
  const cacheKey = `${o.undirected ? 1 : 0}${o.selfLoops ? 1 : 0}${o.keepZero ? 1 : 0}`
  let byData = netCache.get(data)
  const hit = byData?.get(cacheKey)
  if (hit) return hit
  const nodes: GNode[] = []
  const byId = new Map<string, GNode>()
  const add = (id: string, label?: string, group?: string, value: number | null = null): GNode => {
    let n = byId.get(id)
    if (!n) {
      n = { id, label: label ?? id, group, value, index: nodes.length, inValue: 0, outValue: 0, degree: 0 }
      byId.set(id, n)
      nodes.push(n)
    } else {
      if (label !== undefined && n.label === n.id) n.label = label
      if (group !== undefined && n.group === undefined) n.group = group
      if (value !== null) n.value = (n.value ?? 0) + value
    }
    return n
  }
  for (const n of data.nodes ?? []) add(n.id, n.label, n.group, n.value)
  const weighted = data.links.some((l) => l.value !== null)
  const dropped = { nulls: 0, negatives: 0, zeros: 0, duplicates: 0, selfLoops: 0 }
  const links = new Map<string, GLink>()
  for (const l of data.links) {
    add(l.source)
    add(l.target)
    const value = weighted ? l.value : 1
    if (value === null) { dropped.nulls++; continue }
    if (value < 0) { dropped.negatives++; continue }
    if (value === 0 && !o.keepZero) { dropped.zeros++; continue }
    if (l.source === l.target && !o.selfLoops) { dropped.selfLoops++; continue }
    const [s, t] = o.undirected && l.target < l.source ? [l.target, l.source] : [l.source, l.target]
    const key = `${s}\u0000${t}`
    const prev = links.get(key)
    if (prev) { prev.value += value; dropped.duplicates++ }
    else links.set(key, { key: `${s}→${t}`, source: s, target: t, value })
  }
  const out = [...links.values()]
  for (const l of out) {
    const s = byId.get(l.source)!, t = byId.get(l.target)!
    s.outValue += l.value
    t.inValue += l.value
    s.degree++
    if (t !== s) t.degree++
  }
  const groups = [...new Set(nodes.map((n) => n.group).filter((g): g is string => g !== undefined))]
  const net: Net = { nodes, links: out, byId, weighted, groups, dropped }
  if (!byData) netCache.set(data, (byData = new Map()))
  byData.set(cacheKey, net)
  return net
}

/** The first directed cycle found, as a list of ids that starts and ends at the same node. */
export function findCycle(links: ReadonlyArray<{ source: string; target: string }>): string[] | null {
  const out = new Map<string, string[]>()
  for (const l of links) {
    const adj = out.get(l.source)
    if (adj) adj.push(l.target)
    else out.set(l.source, [l.target])
  }
  const state = new Map<string, 1 | 2>()
  for (const start of out.keys()) {
    if (state.has(start)) continue
    const stack: { id: string; next: number }[] = [{ id: start, next: 0 }]
    state.set(start, 1)
    while (stack.length) {
      const top = stack[stack.length - 1]!
      const adj = out.get(top.id) ?? []
      if (top.next < adj.length) {
        const nxt = adj[top.next++]!
        const s = state.get(nxt)
        if (s === 1) {
          const from = stack.findIndex((f) => f.id === nxt)
          return [...stack.slice(from).map((f) => f.id), nxt]
        }
        if (s === undefined) {
          state.set(nxt, 1)
          stack.push({ id: nxt, next: 0 })
        }
      } else {
        state.set(top.id, 2)
        stack.pop()
      }
    }
  }
  return null
}

/** Node colours: by group when any node has one (missing group -> neutral); ungrouped: one per node, or one for all when `ungrouped` is 'single'. */
export function nodeColors(net: Net, theme: Palette, ungrouped: 'each' | 'single' = 'each'): { color: (n: GNode) => string; pattern: (n: GNode) => number; legend: LegendSpec | undefined } {
  if (net.groups.length) {
    const gi = new Map(net.groups.map((g, i) => [g, i]))
    const color = (n: GNode) => (n.group === undefined ? theme.neutral : theme.cat(gi.get(n.group)!))
    const pattern = (n: GNode) => (n.group === undefined ? 7 : gi.get(n.group)! % 8)
    const items = net.groups.map((g, i) => ({ key: g, label: g, color: theme.cat(i), shape: 'circle' as const }))
    return { color, pattern, legend: items.length > 1 ? { items } : undefined }
  }
  if (ungrouped === 'single') return { color: () => theme.cat(0), pattern: () => 0, legend: undefined }
  return { color: (n) => theme.cat(n.index), pattern: (n) => n.index % 8, legend: undefined }
}

export interface Box { x: number; y: number; width: number; height: number }
const overlaps = (a: Box, b: Box, pad: number) =>
  a.x < b.x + b.width + pad && b.x < a.x + a.width + pad && a.y < b.y + b.height + pad && b.y < a.y + a.height + pad

/** Greedy collision-free placement: candidates in priority order, each with fallbacks; returns the chosen option per candidate (or -1). */
export function placeBoxes(cands: Box[][], order: number[], pad = 1, blockers: Box[] = []): number[] {
  const placed: Box[] = [...blockers]
  const pick = cands.map(() => -1)
  for (const i of order) {
    const opts = cands[i]!
    for (let k = 0; k < opts.length; k++) {
      const b = opts[k]!
      if (b.width <= 0) continue
      if (placed.some((p) => overlaps(b, p, pad))) continue
      placed.push(b)
      pick[i] = k
      break
    }
  }
  return pick
}

/** Box of a text label at (x, y) with an anchor and central baseline. */
export function textBox(x: number, y: number, width: number, anchor: 'start' | 'middle' | 'end', size: number): Box {
  const x0 = anchor === 'start' ? x : anchor === 'end' ? x - width : x - width / 2
  return { x: x0, y: y - size * 0.6, width, height: size * 1.2 }
}

/** "1 negative link value omitted; 2 duplicate links summed" */
export function netNotes(d: Net['dropped'], extra: Array<[number, string]> = []): Array<[number, string]> {
  return [
    [d.negatives, `negative link value${d.negatives === 1 ? '' : 's'} omitted`],
    [d.nulls, `link${d.nulls === 1 ? '' : 's'} without a value left out`],
    [d.zeros, `zero-valued link${d.zeros === 1 ? '' : 's'} left out`],
    [d.duplicates, `duplicate link${d.duplicates === 1 ? '' : 's'} summed`],
    [d.selfLoops, `self-loop${d.selfLoops === 1 ? '' : 's'} left out`],
    ...extra,
  ]
}

/** "A → B" */
export const arrow = (a: string, b: string) => `${a} → ${b}`

/** Shared table for graph charts: one row per kept link. */
export function linkTable(net: Net): { columns: string[]; rows: Array<Array<string | number | null>> } {
  return {
    columns: ['Source', 'Target', 'Value'],
    rows: net.links.map((l) => [net.byId.get(l.source)!.label, net.byId.get(l.target)!.label, l.value]),
  }
}

/**
 * Table for graphs that draw every node, linked or not (force network, arc diagram): one
 * row per kept link, then one row per node no kept link touches, with no target or value.
 * A single node, or a graph whose every link was dropped, still has a row per node.
 */
export function nodeLinkTable(net: Net): { columns: string[]; rows: Array<Array<string | number | null>> } {
  const t = linkTable(net)
  const linked = new Set(net.links.flatMap((l) => [l.source, l.target]))
  for (const n of net.nodes) if (!linked.has(n.id)) t.rows.push([n.label, null, null])
  return t
}

