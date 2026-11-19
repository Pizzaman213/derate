import { dsvFormat } from 'd3-dsv'
import { isNumericString, type Row } from './util'

const DELIMITERS = [',', ';', '\t', '|'] as const
const NULLS: Record<string, true> = { '': true, null: true, NA: true, 'N/A': true }

/** The header line of `text`, ending at the first newline that is not inside quotes. */
function headerLine(text: string): string {
  let quoted = false
  for (let i = 0; i < text.length; i++) {
    const c = text[i]
    if (c === '"') quoted = !quoted
    else if (!quoted && (c === '\n' || c === '\r')) return text.slice(0, i)
  }
  return text
}

/** Occurrences of `delim` in `line` outside double quotes. */
function countUnquoted(line: string, delim: string): number {
  let quoted = false
  let n = 0
  for (const c of line) {
    if (c === '"') quoted = !quoted
    else if (!quoted && c === delim) n++
  }
  return n
}

/** The delimiter among , ; tab | that occurs most often in the header line (',' when none does). */
export function detectDelimiter(text: string): string {
  const line = headerLine(text.replace(/^\uFEFF/, ''))
  let best = ','
  let bestCount = 0
  for (const d of DELIMITERS) {
    const n = countUnquoted(line, d)
    if (n > bestCount) {
      best = d
      bestCount = n
    }
  }
  return best
}

/** Coerce one CSV cell: blanks/null markers -> null, plain numbers -> number, true/false -> boolean, else the string. */
export function coerceCell(cell: string): string | number | boolean | null {
  const t = cell.trim()
  if (Object.hasOwn(NULLS, t)) return null
  if (t === 'true') return true
  if (t === 'false') return false
  if (isNumericString(t)) return Number(t)
  return cell
}

/** Parse delimited text into rows keyed by the header; delimiter auto-detected unless given. */
export function parseCSV(text: string, opts: { delimiter?: string } = {}): Row[] {
  const body = text.replace(/^\uFEFF/, '')
  const delimiter = opts.delimiter ?? detectDelimiter(body)
  const table = dsvFormat(delimiter).parseRows(body)
  const header = table[0]
  if (!header) return []
  const columns = header.map((h) => h.trim())
  const rows: Row[] = []
  for (let r = 1; r < table.length; r++) {
    const cells = table[r] ?? []
    if (cells.length === 1 && cells[0] === '') continue // blank line
    const row: Row = {}
    columns.forEach((col, i) => {
      row[col] = coerceCell(cells[i] ?? '')
    })
    rows.push(row)
  }
  return rows
}

/** True when `s` has a newline and its first line contains one of , ; tab |. */
export function looksLikeCSV(s: string): boolean {
  const nl = s.search(/\r?\n/)
  if (nl < 0) return false
  const first = s.slice(0, nl)
  return DELIMITERS.some((d) => first.includes(d))
}

/** Render one value as an RFC 4180 field. */
function field(v: unknown): string {
  let s: string
  if (v === null || v === undefined) s = ''
  else if (v instanceof Date) s = Number.isNaN(v.getTime()) ? '' : v.toISOString()
  else if (typeof v === 'object') s = JSON.stringify(v)
  else s = String(v)
  return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
}

/** Serialise a header and rows to RFC 4180 CSV (CRLF line breaks, quoted where needed). */
export function toCSV(columns: string[], rows: unknown[][]): string {
  return [columns, ...rows].map((r) => r.map(field).join(',')).join('\r\n')
}
