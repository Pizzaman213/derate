import { z } from 'zod'
import { fields } from '../../core'
import { mapOption } from '../_shared/GeoCharts'

/** One dot per row. */
export const input = fields.rows({
  lon: fields.numOrNull('longitude in degrees; rows without a valid lon/lat are skipped and noted'),
  lat: fields.numOrNull('latitude in degrees'),
  category: fields.optLabel('colour group, listed in the legend'),
}, 'rows of { lon, lat, category? }')

export const options = z.object({
  map: mapOption,
  radius: z.number().positive().optional().describe('dot radius in px (default: 3.5 down to 1.25 as the count grows)'),
  maxCategories: z.number().int().min(1).max(12).default(8).describe('categories beyond this many (least frequent first) are merged into "Other"'),
})

export type DotMapRow = z.output<typeof input>[number]
export type DotMapOptions = z.output<typeof options>
