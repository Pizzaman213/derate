import { z } from 'zod'
import { fields } from '../../core'
import { colorScaleOption } from '../_shared/GeoCharts'

/** One row per US state (or DC). */
export const input = fields.rows({
  state: fields.label('US state or DC: USPS postal code, 2-digit FIPS code or name'),
  value: fields.numOrNull('the value that colours the tile; null leaves it grey'),
}, 'rows of { state, value }')

export const options = z.object({
  scale: colorScaleOption,
  valueLabel: z.string().optional().describe('what the value is, shown above the colour legend and in tooltips'),
  showValues: z.boolean().default(false).describe('print each value under its postal code when the tiles are big enough'),
})

export type HexTileRow = z.output<typeof input>[number]
export type HexTileOptions = z.output<typeof options>
