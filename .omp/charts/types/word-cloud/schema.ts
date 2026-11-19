import { z } from 'zod'
import { fields } from '../../core'

/**
 * One row per word or phrase. Duplicate texts are summed (and noted). Prose is accepted
 * too and counted into rows -- see `adapt` in index.ts.
 */
export const input = fields.rows({
  text: fields.label('the word or phrase'),
  value: fields.numOrNull('its weight (count, frequency, score); zero, negative or null leaves it out'),
  group: fields.optLabel('optional colour group with a legend, e.g. sentiment'),
}, 'rows of { text, value, group? }, or prose to count')

export const options = z.object({
  maxWords: z.number().int().positive().default(150).describe('largest words kept; the rest are left out with a note'),
  minFontSize: z.number().positive().optional().describe('px for the smallest value; never below the tick size (default: the tick size)'),
  maxFontSize: z.number().positive().optional().describe('px for the largest value (default: fitted so the words fill about 40% of the frame, shrunk while more than 5% do not fit)'),
  rotate: z.enum(['none', 'mixed']).default('none').describe("'mixed' turns about a third of the words 90° (never the largest)"),
  colorBy: z.enum(['cycle', 'single']).default('cycle').describe('palette cycle, or one colour; a `group` column always colours by group'),
  stopwords: z.array(z.string()).default([]).describe('extra words to drop when counting prose'),
})

export type WordRow = z.output<typeof input>[number]
export type WordCloudOptions = z.output<typeof options>
