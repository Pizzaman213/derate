// Field schemas every chart's input is built from, so CSV strings coerce the same way
// everywhere and every error reads the same ("field 'value' must be a number, got 'abc'
// at row 12" -- see validate.ts).

import { z } from 'zod'
import { parseDate } from '../time/parse'

// Each field is ONE transform over z.unknown(): zod's preprocess/optional/transform
// chains cost ~3x as much per cell, which is the whole validation budget at 50k rows.
// Failures are reported as zod 'invalid_type' issues so validate.ts words them
// ("field 'value' must be a number, got 'abc' at row 12" / "... is missing ...").

type Ctx = z.RefinementCtx
const fail = (ctx: Ctx, expected: string, v: unknown) => {
  ctx.addIssue({ code: z.ZodIssueCode.invalid_type, expected: expected as z.ZodParsedType, received: (v === undefined ? 'undefined' : v === null ? 'null' : typeof v) as z.ZodParsedType, message: expected })
  return z.NEVER
}
const THOUSANDS = /(?<=\d),(?=\d{3}\b)/g
/** number | numeric string -> number; '' / null / NaN -> null; anything else -> undefined (invalid). */
function toNum(v: unknown): number | null | undefined {
  if (typeof v === 'number') return Number.isFinite(v) ? v : Number.isNaN(v) ? null : undefined
  if (v === null || v === undefined) return null
  if (typeof v !== 'string') return undefined
  const t = v.trim()
  if (t === '') return null
  const n = Number(t.includes(',') ? t.replace(THOUSANDS, '') : t)
  return Number.isFinite(n) ? n : undefined
}
let PARSE_TZ: string | undefined
/** Run a (synchronous) parse with tz-less dates read as wall time in `timeZone`. */
export function parsingIn<T>(timeZone: string | undefined, fn: () => T): T {
  const prev = PARSE_TZ
  PARSE_TZ = timeZone
  try {
    return fn()
  } finally {
    PARSE_TZ = prev
  }
}
function toDate(v: unknown): Date | null | undefined {
  if (v instanceof Date) return Number.isNaN(+v) ? undefined : v
  if (v === null || v === undefined || v === '') return null
  return parseDate(v, PARSE_TZ ? { timeZone: PARSE_TZ } : {}) ?? undefined
}

/** What each field helper accepts. */
export type FieldKind = 'number' | 'number?' | 'date' | 'date?' | 'label' | 'label?'
const KINDS = new WeakMap<z.ZodTypeAny, FieldKind>()
const tag = <T extends z.ZodTypeAny>(kind: FieldKind, schema: T): T => {
  KINDS.set(schema, kind)
  return schema
}
/** The kind of a field schema built by these helpers, or undefined. */
export function fieldKind(schema: z.ZodTypeAny): FieldKind | undefined {
  return KINDS.get(schema)
}

/** A finite number; numeric strings ("1,234.5") coerce. */
export const num = (description?: string) =>
  tag('number', z.unknown().transform((v, ctx): number => {
    const n = toNum(v)
    return typeof n === 'number' ? n : fail(ctx, 'number', v)
  }).describe(description ?? 'number'))
/** A number or null/missing (drawn as a gap / skipped). */
export const numOrNull = (description?: string) =>
  tag('number?', z.unknown().transform((v, ctx): number | null => {
    const n = toNum(v)
    return n === undefined ? fail(ctx, 'number', v) : n
  }).describe(description ?? 'number or null'))
/** A date: Date, epoch (s or ms), ISO 8601, or a common written form; see time/parse. */
export const date = (description?: string) =>
  tag('date', z.unknown().transform((v, ctx): Date => {
    const d = toDate(v)
    return d instanceof Date ? d : fail(ctx, 'date', v)
  }).describe(description ?? 'date'))
export const dateOrNull = (description?: string) =>
  tag('date?', z.unknown().transform((v, ctx): Date | null => {
    const d = toDate(v)
    return d === undefined ? fail(ctx, 'date', v) : d
  }).describe(description ?? 'date or null'))
/** A label: strings, numbers stringified; null/missing becomes "(blank)". */
export const label = (description?: string) =>
  tag('label', z.unknown().transform((v, ctx): string => {
    if (typeof v === 'string') return v
    if (typeof v === 'number' || typeof v === 'boolean') return String(v)
    if (v === null || v === undefined) return '(blank)'
    return fail(ctx, 'text', v)
  }).describe(description ?? 'label'))
/** An optional label (series, group). */
export const optLabel = (description?: string) =>
  tag('label?', z.unknown().transform((v, ctx): string | undefined => {
    if (typeof v === 'string') return v === '' ? undefined : v
    if (typeof v === 'number' || typeof v === 'boolean') return String(v)
    if (v === null || v === undefined) return undefined
    return fail(ctx, 'text', v)
  }).describe(description ?? 'optional label'))
/** Rows input. */
export const rows = <T extends z.ZodRawShape>(shape: T, description?: string) => z.array(z.object(shape).passthrough()).describe(description ?? 'array of rows')

const fn = () => z.custom<(...args: never[]) => unknown>((v) => typeof v === 'function', { message: 'function' }).optional()

/** Options every chart accepts (merged under each chart's own options). */
export const baseOptions = z.object({
  width: z.number().positive().optional().describe('px; default: the container width'),
  height: z.number().positive().optional().describe('px; default: the container height, else width x aspect'),
  theme: z.enum(['auto', 'light', 'dark', 'high-contrast']).optional().describe('default auto: follows [data-theme] / media queries'),
  title: z.string().optional().describe('visible title and the start of the aria-label'),
  description: z.string().optional().describe('appended to the aria summary'),
  fields: z.record(z.string()).optional().describe("canonical field -> your column, e.g. { x: 'date', y: 'revenue', series: 'region' }"),
  locale: z.string().optional().describe('BCP 47 locale for numbers and dates (default en-US)'),
  currency: z.string().length(3).optional().describe('ISO 4217 code used by valueFormat currency'),
  timeZone: z.string().optional().describe('IANA zone for parsing tz-less dates and formatting (default UTC)'),
  valueFormat: z.string().optional().describe("auto | si | currency | percent | integer | number | a d3-format specifier like '.2f'"),
  animate: z.boolean().optional().describe('default true; always off under prefers-reduced-motion'),
  legend: z.boolean().optional().describe('default true when there is more than one series'),
  tooltip: z.boolean().optional(),
  patterns: z.boolean().optional().describe('pattern fills on top of colour, for print and colour-vision deficiency'),
  zoom: z.boolean().optional().describe('wheel/drag zoom and pan (time and scatter charts)'),
  brush: z.boolean().optional().describe('drag to select a range; reported through onBrush'),
  renderer: z.enum(['auto', 'svg', 'canvas']).optional().describe('auto: canvas above canvasThreshold data marks'),
  canvasThreshold: z.number().int().positive().optional().describe('default 5000'),
  showTable: z.boolean().optional().describe('show the data table under the chart (it is always present for screen readers)'),
  emptyMessage: z.string().optional(),
  seed: z.number().int().optional().describe('seed for jitter and force layouts'),
  xDomain: z.tuple([z.unknown(), z.unknown()]).optional().describe('fix the x domain (shared scales in small multiples)'),
  yDomain: z.tuple([z.number(), z.number()]).optional().describe('fix the y domain'),
  onClick: fn(),
  onHover: fn(),
  onBrush: fn(),
  onLegendToggle: fn(),
  onZoom: fn(),
}).passthrough()
