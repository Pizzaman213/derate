import { AdapterError, columnsOf, keyOf, toNumber, type Row } from './util'

/** How a group of numbers collapses to one. */
export type Agg = 'sum' | 'mean' | 'median' | 'count' | 'min' | 'max' | 'first' | 'last'

/** Collapse numbers with `agg`, ignoring null/undefined/NaN; empty -> 0 for sum/count, null otherwise. */
export function aggregate(values: readonly (number | null | undefined)[], agg: Agg): number | null {
  const xs = values.filter((v): v is number => typeof v === 'number' && !Number.isNaN(v))
  switch (agg) {
    case 'count':
      return xs.length
    case 'sum':
      return xs.reduce((a, b) => a + b, 0)
    default:
      if (xs.length === 0) return null
  }
  switch (agg) {
    case 'mean':
      return xs.reduce((a, b) => a + b, 0) / xs.length
    case 'median': {
      const s = [...xs].sort((a, b) => a - b)
      const mid = s.length >> 1
      return s.length % 2 ? (s[mid] as number) : ((s[mid - 1] as number) + (s[mid] as number)) / 2
    }
    case 'min':
      return xs.reduce((a, b) => (b < a ? b : a))
    case 'max':
      return xs.reduce((a, b) => (b > a ? b : a))
    case 'first':
      return xs[0] as number
    case 'last':
      return xs[xs.length - 1] as number
  }
}

/** Rows bucketed by the values of `by`, in first-seen order. */
export function groupRows(rows: readonly Row[], by: readonly string[]): Row[][] {
  const groups = new Map<string, Row[]>()
  for (const r of rows) {
    const k = by.map((c) => keyOf(r[c])).join('\u0000')
    const g = groups.get(k)
    if (g) g.push(r)
    else groups.set(k, [r])
  }
  return [...groups.values()]
}

/** One row per distinct `by` combination (first-seen order) holding `agg` of `value` under `as`. */
export function groupAggregate(
  rows: readonly Row[],
  opts: { by: string[]; value?: string; agg: Agg; as?: string },
): Row[] {
  const { by, value, agg } = opts
  if (value === undefined && agg !== 'count') throw new AdapterError(`groupAggregate: '${agg}' needs a value column`)
  const as = opts.as ?? value ?? 'count'
  return groupRows(rows, by).map((group) => {
    const out: Row = {}
    const first = group[0] as Row
    for (const c of by) out[c] = first[c]
    out[as] = value === undefined ? group.length : aggregate(group.map((r) => toNumber(r[value])), agg)
    return out
  })
}

/** Scale `value` so each `by` group sums to `to` (default 100) under `as`; an all-zero group stays 0. */
export function normalize(
  rows: readonly Row[],
  opts: { value: string; by?: string; to?: number; as?: string },
): Row[] {
  const { value, by } = opts
  const to = opts.to ?? 100
  const as = opts.as ?? value
  const totals = new Map<string, number>()
  const groupKey = (r: Row): string => (by === undefined ? '' : keyOf(r[by]))
  for (const r of rows) {
    const k = groupKey(r)
    totals.set(k, (totals.get(k) ?? 0) + (toNumber(r[value]) ?? 0))
  }
  return rows.map((r) => {
    const v = toNumber(r[value])
    const total = totals.get(groupKey(r)) ?? 0
    return { ...r, [as]: v === null ? null : total === 0 ? 0 : (v / total) * to }
  })
}

/** Keep the `n` largest rows by `value` (descending) and collapse the rest into one `other` row. */
export function topN(
  rows: readonly Row[],
  opts: { value: string; n: number; other?: string; label?: string },
): Row[] {
  const { value, n } = opts
  const other = opts.other ?? 'Other'
  const sorted = rows
    .map((r, i) => ({ r, i, v: toNumber(r[value]) ?? Number.NEGATIVE_INFINITY }))
    .sort((a, b) => b.v - a.v || a.i - b.i)
    .map((e) => e.r)
  const keep = Math.max(0, Math.floor(n))
  if (sorted.length <= keep) return sorted
  const head = sorted.slice(0, keep)
  const tail = sorted.slice(keep)
  const label =
    opts.label ?? columnsOf(rows).find((c) => c !== value && rows.some((r) => typeof r[c] === 'string')) ?? 'label'
  const rest: Row = { [label]: other, [value]: tail.reduce((s, r) => s + (toNumber(r[value]) ?? 0), 0) }
  return [...head, rest]
}
