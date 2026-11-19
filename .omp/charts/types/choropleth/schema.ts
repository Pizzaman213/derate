import { z } from 'zod'
import { fields } from '../../core'
import { colorScaleOption, mapOption } from '../_shared/GeoCharts'

/** One row per region. Keys: ISO3/ISO2/ISO numeric/name (world) or FIPS/postal/name (US states). */
export const input = fields.rows({
  region: fields.label('region key: ISO 3166 alpha-3, alpha-2, numeric or name (world); FIPS, postal code or name (us-states)'),
  value: fields.numOrNull('the value that colours the region; null leaves it uncoloured'),
}, 'rows of { region, value }')

export const options = z.object({
  map: mapOption,
  scale: colorScaleOption,
  valueLabel: z.string().optional().describe('what the value is, shown above the colour legend and in tooltips'),
})

export type ChoroplethRow = z.output<typeof input>[number]
export type ChoroplethOptions = z.output<typeof options>
