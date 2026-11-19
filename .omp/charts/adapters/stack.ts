import {
  stack,
  stackOffsetDiverging,
  stackOffsetExpand,
  stackOffsetNone,
  stackOffsetSilhouette,
  stackOffsetWiggle,
  stackOrderAscending,
  stackOrderDescending,
  stackOrderInsideOut,
  stackOrderNone,
} from 'd3-shape'
import { distinct } from './reshape'
import { keyOf, toNumber, type Row } from './util'

/** One stacked segment: baseline y0, top y1, its own value and the x's total. */
export type StackPoint = { x: unknown; y0: number; y1: number; value: number; total: number }

/** Stacked series; `layers` run bottom-to-top as `order` stacked them, `keys` keep the input order. */
export type Stacked = { keys: string[]; xs: unknown[]; layers: { key: string; points: StackPoint[] }[] }

/** Stack options: the long-format columns plus d3-shape offset/order. */
export type StackOptions = {
  x: string
  series: string
  value: string
  offset?: 'none' | 'expand' | 'diverging' | 'wiggle' | 'silhouette'
  order?: 'input' | 'ascending' | 'descending' | 'insideOut'
  seriesOrder?: string[]
}

const OFFSETS = {
  none: stackOffsetNone,
  expand: stackOffsetExpand,
  diverging: stackOffsetDiverging,
  wiggle: stackOffsetWiggle,
  silhouette: stackOffsetSilhouette,
}
const ORDERS = {
  input: stackOrderNone,
  ascending: stackOrderAscending,
  descending: stackOrderDescending,
  insideOut: stackOrderInsideOut,
}

/** Stack long rows into layers per series over each distinct x; missing combos and nulls count as 0. */
export function stackRows(rows: readonly Row[], opts: StackOptions): Stacked {
  const xs = distinct(rows, opts.x)
  const found = distinct(rows, opts.series).map((s) => String(s ?? ''))
  const keys = opts.seriesOrder
    ? [...opts.seriesOrder, ...found.filter((s) => !opts.seriesOrder?.includes(s))]
    : found
  const xIndex = new Map(xs.map((x, i) => [keyOf(x), i]))
  const matrix: Map<string, number>[] = xs.map(() => new Map())
  for (const r of rows) {
    const cell = matrix[xIndex.get(keyOf(r[opts.x])) ?? -1]
    if (!cell) continue
    const s = String(r[opts.series] ?? '')
    cell.set(s, (cell.get(s) ?? 0) + (toNumber(r[opts.value]) ?? 0))
  }
  const totals = matrix.map((m) => [...m.values()].reduce((a, b) => a + b, 0))
  const series = stack<Map<string, number>, string>()
    .keys(keys)
    .value((m, k) => m.get(k) ?? 0)
    .offset(OFFSETS[opts.offset ?? 'none'])
    .order(ORDERS[opts.order ?? 'input'])(matrix)
  return {
    keys,
    xs,
    layers: [...series].sort((a, b) => a.index - b.index).map((s) => ({
      key: s.key,
      points: s.map((p, i) => ({
        x: xs[i],
        y0: Number.isFinite(p[0]) ? p[0] : 0,
        y1: Number.isFinite(p[1]) ? p[1] : 0,
        value: matrix[i]?.get(s.key) ?? 0,
        total: totals[i] ?? 0,
      })),
    })),
  }
}
