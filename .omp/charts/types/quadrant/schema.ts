import { z } from 'zod'
import { fields } from '../../core'

/** One point per row. */
export const input = fields.rows({
  label: fields.label('name of the item, printed beside its point where it fits'),
  x: fields.numOrNull('horizontal position; null leaves the item out'),
  y: fields.numOrNull('vertical position; null leaves the item out'),
}, 'rows of { label, x, y }')

const quadrantLabel = (where: string) => z.string().optional().describe(`label for the ${where} quadrant`)

export const options = z.object({
  xMid: z.number().finite().optional().describe('x where the vertical split line sits (default: the median of x)'),
  yMid: z.number().finite().optional().describe('y where the horizontal split line sits (default: the median of y)'),
  q1: quadrantLabel('top-right (high x, high y)'),
  q2: quadrantLabel('top-left (low x, high y)'),
  q3: quadrantLabel('bottom-left (low x, low y)'),
  q4: quadrantLabel('bottom-right (high x, low y)'),
  showLabels: z.boolean().default(true).describe('print point labels where they fit without overlapping'),
  colorBy: z.enum(['quadrant', 'single']).default('quadrant').describe('colour points by their quadrant, or all alike'),
  xTitle: z.string().optional(),
  yTitle: z.string().optional(),
})

export type QuadrantRow = z.output<typeof input>[number]
export type QuadrantOptions = z.output<typeof options>
