import { z } from 'zod'
import { fields } from '../../core'

/** "150;225;300", "150|225|300", "150 225 300" or "[150,225,300]" -> [150, 225, 300]. */
const splitRanges = (v: unknown): unknown => {
  if (v === null || v === undefined || v === '') return undefined
  if (typeof v === 'number') return [v]
  if (typeof v !== 'string') return v
  const t = v.trim()
  // bracketed: a JSON-style list, commas separate; bare: ; | or spaces separate (a comma would read as thousands)
  const parts = t.startsWith('[') ? t.slice(1, t.endsWith(']') ? -1 : undefined).split(',') : t.split(/[;|\s]+/)
  return parts.map((s) => s.trim()).filter(Boolean).map((s) => {
    const n = Number(s.replace(/(?<=\d),(?=\d{3}\b)/g, ''))
    return Number.isFinite(n) ? n : s
  })
}

/** One bullet per row: a measure, an optional target and optional qualitative bands. */
export const input = fields.rows({
  label: fields.label('what is measured'),
  value: fields.numOrNull('the measure (the bar); null draws the bands and target only'),
  target: fields.numOrNull('the comparative measure (the tick)'),
  ranges: z.preprocess(
    splitRanges,
    z.array(z.number({ invalid_type_error: 'number' }).finite()).optional(),
  ).describe('upper bounds of the qualitative bands, e.g. [150, 225, 300] (poor / satisfactory / good); in CSV: "150;225;300"'),
}, 'rows of { label, value, target?, ranges?: number[] }')

export const options = z.object({
  scale: z.enum(['independent', 'shared']).default('independent').describe('independent: each bullet gets its own axis (different units); shared: one axis for all'),
  showValues: z.boolean().default(true).describe('print the value (and target) under each label'),
})

export type BulletRow = z.output<typeof input>[number]
export type BulletOptions = z.output<typeof options>
