// Raw input -> canonical data, or human-readable errors. Rows-shaped charts accept an
// array of objects, CSV/TSV text, column arrays, or array-of-arrays, with an optional
// field mapping; everything then goes through the chart's zod schema.

import type { ZodIssue, ZodType, ZodTypeDef } from 'zod'
import { toRows, mapFields, AdapterError } from '../adapters/rows'
import { parsingIn } from './fields'
import type { ValidationIssue } from './types'

const show = (v: unknown): string => {
  if (v === undefined) return 'nothing'
  if (v === null) return 'null'
  if (typeof v === 'string') return `'${v.length > 40 ? v.slice(0, 40) + '…' : v}'`
  if (typeof v === 'number' || typeof v === 'boolean') return String(v)
  if (v instanceof Date) return Number.isNaN(+v) ? 'an invalid date' : v.toISOString()
  if (Array.isArray(v)) return `a list of ${v.length}`
  return 'an object'
}
const article = (t: string) => (/^[aeiou]/.test(t) ? `an ${t}` : `a ${t}`)

function lookup(root: unknown, path: Array<string | number>): unknown {
  let cur = root
  for (const k of path) {
    if (cur === null || typeof cur !== 'object') return undefined
    cur = (cur as Record<string | number, unknown>)[k]
  }
  return cur
}

function pathText(path: Array<string | number>): { field: string; row: number | null } {
  if (typeof path[0] === 'number') {
    const rest = path.slice(1)
    return { row: path[0] + 1, field: rest.map((k, i) => (typeof k === 'number' ? `[${k}]` : i === 0 ? k : `.${k}`)).join('') }
  }
  return { row: null, field: path.map((k, i) => (typeof k === 'number' ? `[${k}]` : i === 0 ? k : `.${k}`)).join('') }
}

/** One zod issue as a sentence a person can act on. */
export function describeIssue(issue: ZodIssue, input: unknown): ValidationIssue {
  const { field, row } = pathText(issue.path)
  const at = row !== null ? ` at row ${row}` : ''
  const got = lookup(input, issue.path)
  const name = field ? `field '${field}'` : row !== null ? `row ${row}` : 'the data'
  let message: string
  if (issue.code === 'invalid_type') {
    const expected = issue.message && !/^(Expected|Required)/.test(issue.message) ? issue.message : String(issue.expected)
    const want = expected === 'string' ? 'text' : expected
    if (got === undefined || issue.received === 'undefined') message = `${name} is missing${at}`
    else message = `${name} must be ${article(want)}, got ${show(got)}${at}`
  } else if (issue.code === 'invalid_date') {
    message = `${name} must be a date, got ${show(got)}${at} (try ISO 8601 like 2026-03-01)`
  } else if (issue.code === 'invalid_enum_value') {
    message = `${name} must be one of ${issue.options.map((o) => `'${String(o)}'`).join(', ')}, got ${show(got)}${at}`
  } else if (issue.code === 'too_small' && issue.type === 'array') {
    message = `${name} needs at least ${issue.minimum} item(s)${at}`
  } else if (issue.code === 'too_small' || issue.code === 'too_big') {
    message = `${name} ${issue.message.replace(/^(Number|String|Array) /, '').toLowerCase()}, got ${show(got)}${at}`
  } else if (issue.code === 'invalid_union') {
    message = `${name} has an unrecognised shape${at}, got ${show(got)}`
  } else {
    message = `${name}: ${issue.message}${at}`
  }
  return { path: issue.path.join('.'), message }
}

export interface Prepared<D> {
  ok: boolean
  data?: D
  errors: ValidationIssue[]
}

/** Coerce raw input through the adapters and the chart schema. */
export function prepare<D>(
  raw: unknown,
  schema: ZodType<D, ZodTypeDef, unknown>,
  shape: 'rows' | 'object',
  fields: Record<string, string> | undefined,
  adapt?: (raw: unknown) => unknown,
  timeZone?: string,
): Prepared<D> {
  let input: unknown = raw
  try {
    if (shape === 'rows') {
      if (raw === null || raw === undefined) input = []
      else {
        const rows = toRows(raw)
        if (rows === null) return { ok: false, errors: [{ path: '', message: `the data must be a list of rows, CSV text, or column arrays, got ${show(raw)}` }] }
        input = fields && Object.keys(fields).length ? mapFields(rows, fields) : rows
      }
    }
    else if (fields && Object.keys(fields).length && raw !== null && typeof raw !== 'number') {
      // object-shaped charts that also take rows (trees from id/parent, graphs from links)
      const rows = Array.isArray(raw) || typeof raw === 'string' ? toRows(raw) : null
      if (rows) input = mapFields(rows, fields)
    }
    if (adapt) input = adapt(input)
  } catch (e) {
    return { ok: false, errors: [{ path: '', message: e instanceof AdapterError || e instanceof Error ? e.message : String(e) }] }
  }
  const r = parsingIn(timeZone, () => schema.safeParse(input))
  if (r.success) return { ok: true, data: r.data, errors: [] }
  const seen = new Set<string>()
  const errors: ValidationIssue[] = []
  for (const issue of r.error.issues) {
    const d = describeIssue(issue, input)
    if (seen.has(d.message)) continue
    seen.add(d.message)
    errors.push(d)
  }
  return { ok: false, errors }
}
