import { looksLikeCSV, parseCSV } from './csv'
import { AdapterError, columnsOf, isRecord, type Row } from './util'

export { AdapterError, type Row } from './util'

/** The type a column holds, as inferred from its non-empty cells. */
export type FieldType = 'number' | 'date' | 'string' | 'boolean' | 'mixed' | 'empty'

/** Column arrays `{a: [..], b: [..]}` -> rows; throws AdapterError when lengths differ. */
export function columnsToRows(columns: Record<string, readonly unknown[]>): Row[] {
  const names = Object.keys(columns)
  const first = names[0]
  if (first === undefined) return []
  const length = columns[first]?.length ?? 0
  for (const name of names) {
    const n = columns[name]?.length ?? 0
    if (n !== length) throw new AdapterError(`column '${name}' has ${n} values but column '${first}' has ${length}`)
  }
  const rows: Row[] = []
  for (let i = 0; i < length; i++) {
    const row: Row = {}
    for (const name of names) row[name] = columns[name]?.[i]
    rows.push(row)
  }
  return rows
}

/** Rows -> column arrays over `columns` (default: every column, first-seen); missing cells -> null. */
export function rowsToColumns(rows: readonly Row[], columns?: string[]): Record<string, unknown[]> {
  const out: Record<string, unknown[]> = {}
  for (const c of columns ?? columnsOf(rows)) out[c] = rows.map((r) => (Object.hasOwn(r, c) ? r[c] : null))
  return out
}

/** Array of arrays whose first row is the header -> rows; short rows padded with null. */
function tableToRows(table: readonly unknown[][]): Row[] {
  const [header, ...body] = table
  if (!header) return []
  const names = header.map((h) => String(h))
  return body.map((cells) => {
    const row: Row = {}
    names.forEach((n, i) => {
      row[n] = i < cells.length ? cells[i] : null
    })
    return row
  })
}

/** Accept rows, CSV/TSV text, column arrays or a header-first table; anything else -> null. */
export function toRows(raw: unknown): Row[] | null {
  if (typeof raw === 'string') return looksLikeCSV(raw) ? parseCSV(raw) : null
  if (Array.isArray(raw)) {
    if (raw.length === 0) return []
    if (raw.every(isRecord)) return raw
    if (raw.every((r): r is unknown[] => Array.isArray(r))) return tableToRows(raw)
    return null
  }
  if (isRecord(raw)) {
    const values = Object.values(raw)
    if (values.length > 0 && values.every((v) => Array.isArray(v))) {
      return columnsToRows(raw as Record<string, unknown[]>)
    }
  }
  return null
}

/** Set canonical keys from source columns (`{x: 'date', y: 'revenue'}`), keeping every other column. */
export function mapFields(rows: readonly Row[], mapping: Record<string, string>): Row[] {
  const present = columnsOf(rows)
  for (const source of Object.values(mapping)) {
    if (!present.includes(source)) {
      throw new AdapterError(`field mapping: column '${source}' not found (columns: ${present.join(', ')})`)
    }
  }
  const entries = Object.entries(mapping)
  return rows.map((r) => {
    const out: Row = { ...r }
    for (const [canonical, source] of entries) out[canonical] = Object.hasOwn(r, source) ? r[source] : null
    return out
  })
}

const DATE_PATTERNS = [
  /^\d{4}-\d{2}(?:-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?)?$/, // ISO 8601
  /^\d{4}\/\d{1,2}\/\d{1,2}$/, // 2024/01/31
  /^\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}$/, // 01/31/2024, 31.01.2024
  /^(?:\d{1,2} )?(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?(?: \d{1,2},?)? \d{4}$/i, // Jan 31, 2024 / 31 January 2024
]

/** True when `s` is a date in ISO 8601 or a common written form. */
export function isDateString(s: string): boolean {
  const t = s.trim()
  return DATE_PATTERNS.some((p) => p.test(t))
}

/** The type of one non-empty cell, or null when the cell is empty. */
function cellType(v: unknown): Exclude<FieldType, 'mixed' | 'empty'> | null {
  if (v === null || v === undefined || v === '') return null
  if (typeof v === 'number') return Number.isNaN(v) ? null : 'number'
  if (typeof v === 'boolean') return 'boolean'
  if (v instanceof Date) return Number.isNaN(v.getTime()) ? null : 'date'
  if (typeof v === 'string' && isDateString(v)) return 'date'
  return 'string'
}

/** Infer each column's type from its non-empty cells: one type -> it, several -> 'mixed', none -> 'empty'. */
export function inferFieldTypes(rows: readonly Row[]): Record<string, FieldType> {
  const out: Record<string, FieldType> = {}
  for (const c of columnsOf(rows)) {
    let seen: FieldType = 'empty'
    for (const r of rows) {
      const t = cellType(r[c])
      if (t === null) continue
      if (seen === 'empty') seen = t
      else if (seen !== t) {
        seen = 'mixed'
        break
      }
    }
    out[c] = seen
  }
  return out
}
