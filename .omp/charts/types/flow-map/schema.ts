import { z } from 'zod'
import { fields } from '../../core'
import { mapOption } from '../_shared/GeoCharts'

/** One flow per row: between two regions, or between two lon/lat points. */
export const input = fields.rows({
  from: fields.optLabel('origin region key (country ISO3/ISO2/numeric/name, or US state FIPS/postal/name)'),
  to: fields.optLabel('destination region key'),
  fromLon: fields.numOrNull('origin longitude, used when there is no `from`'),
  fromLat: fields.numOrNull('origin latitude'),
  toLon: fields.numOrNull('destination longitude, used when there is no `to`'),
  toLat: fields.numOrNull('destination latitude'),
  value: fields.numOrNull('line width is proportional to it; null or negative skips the row'),
}, 'rows of { from, to | fromLon, fromLat, toLon, toLat, value }')

export const options = z.object({
  map: mapOption,
  arc: z.enum(['curved', 'great-circle']).default('curved').describe("'curved': a bowed arc that keeps A->B and B->A apart; 'great-circle': the shortest route on the globe"),
  colorBy: z.enum(['single', 'origin']).default('single').describe('one colour, or one per origin (legend toggles origins)'),
  maxWidth: z.number().positive().optional().describe('stroke width of the largest flow in px (default 2.5% of the shorter side, 3-12 px)'),
  showLabels: z.boolean().default(true).describe('name the busiest places where there is room'),
  valueLabel: z.string().optional().describe('what the value is, used in tooltips'),
})

export type FlowMapRow = z.output<typeof input>[number]
export type FlowMapOptions = z.output<typeof options>
