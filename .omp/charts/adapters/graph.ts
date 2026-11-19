import { AdapterError, toNumber, type Row } from './util'

/** A directed weighted graph: nodes by id, links between ids. */
export type Graph = { nodes: { id: string }[]; links: { source: string; target: string; value: number }[] }

/** Edge rows -> graph; nodes in first-seen order, duplicate links summed, value 1 when no value column is given. */
export function toGraph(rows: readonly Row[], opts: { source: string; target: string; value?: string }): Graph {
  const nodes: { id: string }[] = []
  const seen = new Set<string>()
  const links = new Map<string, { source: string; target: string; value: number }>()
  const node = (id: string): void => {
    if (!seen.has(id)) {
      seen.add(id)
      nodes.push({ id })
    }
  }
  rows.forEach((r, i) => {
    const s = r[opts.source]
    const t = r[opts.target]
    if (s === null || s === undefined || s === '') throw new AdapterError(`row ${i + 1}: source is empty`)
    if (t === null || t === undefined || t === '') throw new AdapterError(`row ${i + 1}: target is empty`)
    const source = String(s)
    const target = String(t)
    node(source)
    node(target)
    const value = opts.value === undefined ? 1 : (toNumber(r[opts.value]) ?? 0)
    const key = `${source}\u0000${target}`
    const link = links.get(key)
    if (link) link.value += value
    else links.set(key, { source, target, value })
  })
  return { nodes, links: [...links.values()] }
}

/** An n x n adjacency matrix (row = source, column = target) -> graph; zero cells are not links. */
export function graphFromMatrix(names: string[], matrix: number[][]): Graph {
  const n = names.length
  if (matrix.length !== n) throw new AdapterError(`matrix has ${matrix.length} rows but there are ${n} names`)
  const links: Graph['links'] = []
  matrix.forEach((row, i) => {
    if (row.length !== n) throw new AdapterError(`matrix row ${i} has ${row.length} values but there are ${n} names`)
    row.forEach((v, j) => {
      if (v !== 0 && Number.isFinite(v)) links.push({ source: names[i] as string, target: names[j] as string, value: v })
    })
  })
  return { nodes: names.map((id) => ({ id })), links }
}

/** Graph -> names (node order, then unknown link endpoints) and a summed adjacency matrix. */
export function matrixFromGraph(graph: Graph): { names: string[]; matrix: number[][] } {
  const names = graph.nodes.map((n) => n.id)
  const index = new Map(names.map((n, i) => [n, i]))
  for (const l of graph.links)
    for (const id of [l.source, l.target])
      if (!index.has(id)) {
        index.set(id, names.length)
        names.push(id)
      }
  const matrix = names.map(() => names.map(() => 0))
  for (const l of graph.links) {
    const row = matrix[index.get(l.source) as number] as number[]
    const j = index.get(l.target) as number
    row[j] = (row[j] ?? 0) + l.value
  }
  return { names, matrix }
}

/** True when the directed graph has a cycle (a self-loop counts). */
export function hasCycle(graph: Graph): boolean {
  const out = new Map<string, string[]>()
  for (const l of graph.links) {
    const adj = out.get(l.source)
    if (adj) adj.push(l.target)
    else out.set(l.source, [l.target])
  }
  const state = new Map<string, 1 | 2>() // 1 = on the stack, 2 = finished
  for (const start of out.keys()) {
    if (state.has(start)) continue
    const stack: { id: string; next: number }[] = [{ id: start, next: 0 }]
    state.set(start, 1)
    while (stack.length > 0) {
      const top = stack[stack.length - 1] as { id: string; next: number }
      const adj = out.get(top.id) ?? []
      if (top.next < adj.length) {
        const nxt = adj[top.next++] as string
        const s = state.get(nxt)
        if (s === 1) return true
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
  return false
}
