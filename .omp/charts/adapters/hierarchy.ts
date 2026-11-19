import { AdapterError, isRecord, toNumber, type Row } from './util'

/** A tree node: a name, an optional value, optional children, and any extra fields. */
export type TreeNode = { name: string; value?: number; children?: TreeNode[]; [k: string]: unknown }

/** One node of a flattened tree; `id` is the slash-joined path of names from the root. */
export type FlatNode = { id: string; parent: string | null; name: string; depth: number; value: number }

/** One root as-is, several under a synthetic root named 'All'. */
function rootOf(roots: TreeNode[]): TreeNode {
  return roots.length === 1 ? (roots[0] as TreeNode) : { name: 'All', children: roots }
}

/** Build a tree from id/parent rows; several roots get a synthetic 'All' root; cycles and unknown parents throw. */
export function stratifyRows(
  rows: readonly Row[],
  opts: { id: string; parent: string; value?: string; name?: string },
): TreeNode {
  const nodes = new Map<string, TreeNode>()
  const parentOf = new Map<string, string | null>()
  const rowOf = new Map<string, number>()
  rows.forEach((r, i) => {
    const raw = r[opts.id]
    if (raw === null || raw === undefined || raw === '') throw new AdapterError(`row ${i + 1}: id is empty`)
    const id = String(raw)
    if (nodes.has(id)) throw new AdapterError(`row ${i + 1}: duplicate id '${id}'`)
    const p = r[opts.parent]
    const node: TreeNode = { name: String(opts.name !== undefined ? (r[opts.name] ?? id) : id), id }
    if (opts.value !== undefined) {
      const v = toNumber(r[opts.value])
      if (v !== null) node.value = v
    }
    nodes.set(id, node)
    parentOf.set(id, p === null || p === undefined || p === '' ? null : String(p))
    rowOf.set(id, i + 1)
  })
  for (const [id, p] of parentOf) {
    if (p !== null && !nodes.has(p)) throw new AdapterError(`row ${rowOf.get(id)}: parent '${p}' not found`)
  }
  const reachesRoot = new Set<string>()
  for (const start of nodes.keys()) {
    const path: string[] = []
    const onPath = new Set<string>()
    let cur: string | null = start
    while (cur !== null && !reachesRoot.has(cur)) {
      if (onPath.has(cur)) {
        const loop = [...path.slice(path.indexOf(cur)), cur].map((s) => `'${s}'`).join(' -> ')
        throw new AdapterError(`row ${rowOf.get(cur)}: cycle ${loop}`)
      }
      path.push(cur)
      onPath.add(cur)
      cur = parentOf.get(cur) ?? null
    }
    for (const id of path) reachesRoot.add(id)
  }
  const roots: TreeNode[] = []
  for (const [id, node] of nodes) {
    const p = parentOf.get(id) ?? null
    if (p === null) roots.push(node)
    else {
      const parent = nodes.get(p) as TreeNode
      ;(parent.children ??= []).push(node)
    }
  }
  return rootOf(roots)
}

/** Build a tree from path strings ('a/b/c'); a repeated path sums its values; several tops share an 'All' root. */
export function pathsToTree(
  rows: readonly Row[],
  opts: { path: string; separator?: string; value: string },
): TreeNode {
  const sep = opts.separator ?? '/'
  const top: TreeNode = { name: 'All', children: [] }
  const index = new Map<TreeNode, Map<string, TreeNode>>()
  rows.forEach((r, i) => {
    const parts = String(r[opts.path] ?? '')
      .split(sep)
      .filter((s) => s !== '')
    if (parts.length === 0) throw new AdapterError(`row ${i + 1}: path is empty`)
    let node = top
    for (const part of parts) {
      let kids = index.get(node)
      if (!kids) index.set(node, (kids = new Map()))
      let child = kids.get(part)
      if (!child) {
        child = { name: part }
        kids.set(part, child)
        ;(node.children ??= []).push(child)
      }
      node = child
    }
    const v = toNumber(r[opts.value])
    if (v !== null) node.value = (node.value ?? 0) + v
  })
  return rootOf(top.children ?? [])
}

/** Validate a nested `{name, value?, children?}` object and return a deep copy typed as TreeNode. */
export function treeFromNested(obj: unknown): TreeNode {
  const walk = (o: unknown, at: string): TreeNode => {
    if (!isRecord(o)) throw new AdapterError(`${at} must be an object`)
    const { name, value, children } = o
    if (typeof name !== 'string' && typeof name !== 'number') throw new AdapterError(`${at}.name must be a string`)
    const node: TreeNode = { ...o, name: String(name) }
    if (value !== undefined && value !== null) {
      if (typeof value !== 'number' || !Number.isFinite(value)) {
        throw new AdapterError(`${at}.value must be a finite number`)
      }
    } else delete node.value
    if (children !== undefined && children !== null) {
      if (!Array.isArray(children)) throw new AdapterError(`${at}.children must be an array`)
      node.children = children.map((c, i) => walk(c, `${at}.children[${i}]`))
    } else delete node.children
    return node
  }
  return walk(obj, 'root')
}

/** A copy whose every internal node's value is the sum of its children's; a leaf without a value is 0. */
export function sumTree(node: TreeNode): TreeNode {
  if (!node.children || node.children.length === 0) return { ...node, value: node.value ?? 0 }
  const children = node.children.map(sumTree)
  return { ...node, children, value: children.reduce((s, c) => s + (c.value ?? 0), 0) }
}

/** Pre-order list of nodes with path ids, parent ids, depth and value (missing -> 0). */
export function flattenTree(node: TreeNode): FlatNode[] {
  const out: FlatNode[] = []
  const visit = (n: TreeNode, parent: string | null, depth: number): void => {
    const id = parent === null ? n.name : `${parent}/${n.name}`
    out.push({ id, parent, name: n.name, depth, value: n.value ?? 0 })
    for (const c of n.children ?? []) visit(c, id, depth + 1)
  }
  visit(node, null, 0)
  return out
}
