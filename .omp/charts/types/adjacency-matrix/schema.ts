import { z } from 'zod'
import { fields } from '../../core'

const node = z.object({
  id: fields.label('node id (link endpoints refer to it)'),
  label: fields.optLabel('display name (default: the id)'),
  group: fields.optLabel('cluster used by order: cluster and color: group'),
}).passthrough()

/** A link endpoint: required text (numbers become text). */
const endpoint = (description: string) =>
  z.preprocess((v) => (typeof v === 'number' || typeof v === 'boolean' ? String(v) : v), z.string({ invalid_type_error: 'text', required_error: 'text' }).min(1, 'must not be empty')).describe(description)

const link = z.object({
  source: endpoint('row node'),
  target: endpoint('column node'),
  // absent -> 1 (an unweighted link); null or '' -> a link of unknown weight
  value: z.preprocess((v) => (v === undefined ? 1 : v), fields.numOrNull('link weight; default 1')),
}).passthrough()

export const input = z.object({
  nodes: z.array(node).default([]),
  links: z.array(link),
}, { invalid_type_error: '{ nodes, links } graph, link rows or CSV text' }).describe('{ nodes?: [{ id, label?, group? }], links: [{ source, target, value? }] }, or link rows / CSV')

export const options = z.object({
  order: z.enum(['input', 'name', 'degree', 'cluster']).default('input').describe('node order: as given, by name, by number of links, or grouped by `group`'),
  symmetric: z.boolean().default(false).describe('undirected: a link fills both (a, b) and (b, a); links in both directions are summed'),
  color: z.enum(['value', 'group']).default('value').describe('cell colour: a ramp over the link weight, or the group colour for links inside a group'),
  colorScale: z.enum(['linear', 'log']).default('linear').describe('log compresses heavy-tailed weights so light links stay visible'),
  showValues: z.boolean().default(true).describe('print weights in cells when they fit'),
})

export type Graph = z.output<typeof input>
export type AdjacencyOptions = z.output<typeof options>
