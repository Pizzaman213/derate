/** A data row: one record, any columns. */
export type Row = Record<string, unknown>

/** Thrown when input data cannot be adapted; the message names what to change. */
export class AdapterError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'AdapterError'
  }
}

/** True for a plain-object value (not null, an array, or a Date) -- the shape of a Row. */
export function isRecord(v: unknown): v is Row {
  return typeof v === 'object' && v !== null && !Array.isArray(v) && !(v instanceof Date)
}

const NUMERIC = /^[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?$/

/** True when `s` is a plain decimal number literal (sign, decimals, exponent; no thousands separators). */
export function isNumericString(s: string): boolean {
  return NUMERIC.test(s)
}

/** A finite number from a number or a numeric string; anything else -> null. */
export function toNumber(v: unknown): number | null {
  if (typeof v === 'number') return Number.isFinite(v) ? v : null
  if (typeof v === 'string') {
    const t = v.trim()
    return NUMERIC.test(t) ? Number(t) : null
  }
  return null
}

/** A stable identity key for a cell, so 1 and '1' differ and equal Dates match. */
export function keyOf(v: unknown): string {
  if (v instanceof Date) return `date:${v.getTime()}`
  if (v === null || v === undefined) return 'null:'
  return `${typeof v}:${String(v)}`
}

/** Every column name present in any row, in first-seen order. */
export function columnsOf(rows: readonly Row[]): string[] {
  const seen = new Set<string>()
  for (const r of rows) for (const k of Object.keys(r)) seen.add(k)
  return [...seen]
}
