// Phrases for aria-label summaries, so every chart's summary reads the same way:
// "<Chart name>, <what>, <range>, <notable>".

import type { Formatters } from './format'

export interface Labeled {
  label: string
  value: number | null | undefined
}

/** "a, b and 3 more" */
export function listPhrase(items: string[], max = 3): string {
  if (items.length === 0) return 'nothing'
  if (items.length <= max) return items.length === 1 ? items[0]! : `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}`
  return `${items.slice(0, max).join(', ')} and ${items.length - max} more`
}

/** "highest B at 48k, lowest E at 12" over labelled values (nulls ignored). */
export function extremes(items: Labeled[], fmt: Formatters, fmtValue: (v: number) => string = fmt.value): string {
  const ok = items.filter((d): d is Labeled & { value: number } => typeof d.value === 'number' && Number.isFinite(d.value))
  if (!ok.length) return 'no values'
  let hi = ok[0]!, lo = ok[0]!
  for (const d of ok) {
    if (d.value > hi.value) hi = d
    if (d.value < lo.value) lo = d
  }
  if (ok.length === 1) return `${hi.label} at ${fmtValue(hi.value)}`
  if (hi.value === lo.value) return `all at ${fmtValue(hi.value)}`
  return `highest ${hi.label} at ${fmtValue(hi.value)}, lowest ${lo.label} at ${fmtValue(lo.value)}`
}

/** "Jan to Dec 2026" style span of dates. */
export function dateSpan(dates: Date[], fmt: Formatters): string {
  const ok = dates.filter((d) => d instanceof Date && !Number.isNaN(+d))
  if (!ok.length) return 'no dates'
  let a = ok[0]!, b = ok[0]!
  for (const d of ok) {
    if (+d < +a) a = d
    if (+d > +b) b = d
  }
  const span = +b - +a
  const g = span > 3 * 365 * 864e5 ? 'year' : span > 60 * 864e5 ? 'month' : span > 2 * 864e5 ? 'day' : 'minute'
  return +a === +b ? fmt.date(a, g === 'minute' ? 'day' : g) : `${fmt.date(a, g)} to ${fmt.date(b, g)}`
}

/** "3 series" / "1 series". */
export function count(n: number, noun: string, plural = `${noun}s`): string {
  return `${n} ${n === 1 ? noun : plural}`
}
