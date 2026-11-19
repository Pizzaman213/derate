// What the data looks like: rows (every column's type, cardinality, nulls, sign and
// order), or empty, or something else. Every chart in the pack takes rows, so that is
// the only structure the chooser keys on.

import { toRows } from '../adapters/rows'
import { parseDate, isDateLike } from '../time/parse'

export type ColumnType = 'number' | 'date' | 'category' | 'boolean' | 'empty'
export interface Column {
  name: string
  type: ColumnType
  distinct: number
  nulls: number
  negatives: number
  /** Values already in ascending order (dates/numbers), ignoring nulls. */
  ascending: boolean
  /** Longest label in characters (categories). */
  maxLen: number
  /** Every row has a different value. */
  unique: boolean
}
export type DataKind = 'rows' | 'empty' | 'unknown'
export interface DataShape {
  kind: DataKind
  rows: Record<string, unknown>[]
  count: number
  columns: Column[]
  numeric: Column[]
  temporal: Column[]
  categorical: Column[]
}

function columnOf(name: string, rows: Record<string, unknown>[]): Column {
  let num = 0, date = 0, bool = 0, str = 0, nulls = 0, negatives = 0, maxLen = 0
  const seen = new Set<string>()
  let asc = true, prev = -Infinity
  for (const r of rows) {
    const v = r[name]
    if (v === null || v === undefined || v === '' || (typeof v === 'number' && Number.isNaN(v))) { nulls++; continue }
    seen.add(String(v))
    if (typeof v === 'boolean') { bool++; continue }
    if (typeof v === 'number') {
      num++
      if (v < 0) negatives++
      if (v < prev) asc = false
      prev = v
      continue
    }
    if (v instanceof Date || (typeof v === 'string' && isDateLike(v))) {
      date++
      const t = +(v instanceof Date ? v : parseDate(v) ?? NaN)
      if (t < prev) asc = false
      prev = t
      continue
    }
    if (typeof v === 'string') {
      const n = Number(v.replace(/,/g, ''))
      if (v.trim() !== '' && Number.isFinite(n) && /^-?[\d,.]+(e-?\d+)?$/i.test(v.trim())) {
        num++
        if (n < 0) negatives++
        continue
      }
      str++
      maxLen = Math.max(maxLen, v.length)
    }
  }
  const filled = rows.length - nulls
  const type: ColumnType = filled === 0 ? 'empty' : num === filled ? 'number' : date === filled ? 'date' : bool === filled ? 'boolean' : 'category'
  if (type === 'category') for (const r of rows) maxLen = Math.max(maxLen, String(r[name] ?? '').length)
  return { name, type, distinct: seen.size, nulls, negatives, ascending: asc, maxLen, unique: seen.size === filled && filled === rows.length }
}

/** Describe the data so the chooser can match charts to it. */
export function inspect(data: unknown): DataShape {
  const base: DataShape = { kind: 'unknown', rows: [], count: 0, columns: [], numeric: [], temporal: [], categorical: [] }
  if (data === null || data === undefined) return { ...base, kind: 'empty' }
  if (Array.isArray(data) && data.length === 0) return { ...base, kind: 'empty' }
  // a bare list of numbers, or one line of text, is not rows (toRows would read it as one)
  if (Array.isArray(data) && data.every((v) => typeof v === 'number' || v === null)) return base
  if (typeof data === 'string' && !data.split(/\r?\n/)[0]!.match(/[,;\t|]/)) return base
  let rows: Record<string, unknown>[] | null = null
  try { rows = toRows(data) } catch { rows = null }
  if (!rows) return base
  if (!rows.length) return { ...base, kind: 'empty' }
  const names = [...new Set(rows.slice(0, 2000).flatMap((r) => Object.keys(r)))]
  const sample = rows.length > 5000 ? rows.filter((_, i) => i % Math.ceil(rows!.length / 5000) === 0) : rows
  const columns = names.map((n) => columnOf(n, sample))
  const numeric = columns.filter((c) => c.type === 'number')
  const temporal = columns.filter((c) => c.type === 'date')
  const categorical = columns.filter((c) => c.type === 'category')
  return { ...base, kind: 'rows', rows, count: rows.length, columns, numeric, temporal, categorical }
}
