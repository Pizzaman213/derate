import { z } from 'zod'
import { fields } from '../../core'
import { mapOption } from '../_shared/GeoCharts'

/** One bubble per row, placed at a region's centroid or at lon/lat. */
export const input = fields.rows({
  region: fields.optLabel('region key (country ISO3/ISO2/numeric/name, or US state FIPS/postal/name); placed at its centroid'),
  lon: fields.numOrNull('longitude in degrees, used when there is no region'),
  lat: fields.numOrNull('latitude in degrees, used when there is no region'),
  value: fields.numOrNull('bubble area is proportional to it; null or negative skips the row'),
  label: fields.optLabel('name shown in the tooltip and, when it fits, inside the bubble'),
}, 'rows of { region | lon, lat, value, label? }')

export const options = z.object({
  map: mapOption,
  maxRadius: z.number().positive().optional().describe('radius of the largest bubble in px (default: 7% of the shorter side, 6-36 px)'),
  showLabels: z.boolean().default(true).describe('print labels inside bubbles big enough to hold them'),
  valueLabel: z.string().optional().describe('what the value is, used in tooltips and the size key'),
})

export type BubbleMapRow = z.output<typeof input>[number]
export type BubbleMapOptions = z.output<typeof options>
