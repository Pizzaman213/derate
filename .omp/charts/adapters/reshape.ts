import { aggregate, groupRows, type Agg } from './aggregate'
import { keyOf, toNumber, type Row } from './util'

/** Long -> wide: one row per `index` value, one column per distinct `columns` value holding `agg` of `values`. */
export function pivot(
  rows: readonly Row[],
  opts: { index: string; columns: string; values: string; agg?: Agg },
): Row[] {
  const { index, columns, values } = opts
  const agg = opts.agg ?? 'sum'
  const names: string[] = []
  const seen = new Set<string>()
  for (const r of rows) {
    const name = String(r[columns] ?? '')
    if (!seen.has(name)) {
      seen.add(name)
      names.push(name)
    }
  }
  return groupRows(rows, [index]).map((group) => {
    const out: Row = { [index]: (group[0] as Row)[index] }
    for (const name of names) {
      const cells = group.filter((r) => String(r[columns] ?? '') === name)
      if (cells.length === 0) out[name] = null
      else if (cells.length === 1 && agg !== 'count') out[name] = (cells[0] as Row)[values] ?? null
      else out[name] = aggregate(cells.map((r) => toNumber(r[values])), agg)
    }
    return out
  })
}

/** Wide -> long: one row per (row, column), the column name under `key` and its cell under `value`. */
export function unpivot(
  rows: readonly Row[],
  opts: { id: string[]; key?: string; value?: string; columns?: string[] },
): Row[] {
  const { id } = opts
  const key = opts.key ?? 'series'
  const value = opts.value ?? 'value'
  const out: Row[] = []
  const ids = new Set(id)
  const seen = new Set<string>()
  const discovered: string[] = []
  if (!opts.columns) {
    for (const r of rows)
      for (const c of Object.keys(r))
        if (!ids.has(c) && !seen.has(c)) {
          seen.add(c)
          discovered.push(c)
        }
  }
  const columns = opts.columns ?? discovered
  for (const r of rows) {
    const base: Row = {}
    for (const c of id) base[c] = r[c]
    for (const c of columns) out.push({ ...base, [key]: c, [value]: Object.hasOwn(r, c) ? r[c] : null })
  }
  return out
}

/** Distinct values of `column` in first-seen order (Dates compared by time). */
export function distinct(rows: readonly Row[], column: string): unknown[] {
  const seen = new Set<string>()
  const out: unknown[] = []
  for (const r of rows) {
    const k = keyOf(r[column])
    if (!seen.has(k)) {
      seen.add(k)
      out.push(r[column])
    }
  }
  return out
}
