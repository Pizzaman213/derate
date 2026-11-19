/**
 * Typed access to the sample-dataset registry (`datasets.json`). Reads the registry through
 * a JSON import only -- no fs -- so it works in the browser bundle as well as under node.
 * The rows themselves live in `<name>.json` beside this file; import the one you need.
 */
import registry from './datasets.json'

export const DATASET_NAMES = [
  'sales-by-month',
  'survey-results',
  'server-metrics',
  'population-by-region',
  'ohlc-prices',
] as const

export type DatasetName = (typeof DATASET_NAMES)[number]

export type DatasetKind = 'rows' | 'tree' | 'graph'

export type DatasetIntent =
  | 'compare' | 'rank' | 'composition' | 'trend' | 'timeline' | 'schedule' | 'distribution'
  | 'relationship' | 'correlation' | 'flow' | 'hierarchy' | 'network' | 'geo' | 'kpi'
  | 'change-over-time' | 'deviation' | 'sets'

export type FieldType = 'date' | 'number' | 'category' | 'text' | 'id' | 'boolean'

/** One field as declared in a dataset file's `fields` (and `linkFields` for graphs). */
export interface FieldSpec {
  type: FieldType
  description: string
  unit?: string
  /** Exact number of null values this field holds across the rows. */
  nulls?: number
  /** Null is a legal value (e.g. the root's parent). */
  nullable?: boolean
  /** Tree datasets: the field is set on leaves only. */
  on?: 'leaf'
}

/** One registry row. `rows` counts rows, tree nodes, or graph nodes depending on `kind`. */
export interface DatasetEntry {
  name: DatasetName
  file: string
  description: string
  kind: DatasetKind
  rows: number
  fields: string[]
  intents: DatasetIntent[]
  suggested: string[]
  added: string
}

export interface DatasetRegistry {
  version: 1
  datasets: DatasetEntry[]
}

/** The registry, typed. validate.mjs and datasets.test.ts hold it to these types. */
export const DATASET_REGISTRY = registry as DatasetRegistry

export function isDatasetName(name: string): name is DatasetName {
  return (DATASET_NAMES as readonly string[]).includes(name)
}

/** The registry entry for `name`; throws on a name the registry does not list. */
export function datasetEntry(name: string): DatasetEntry {
  const entry = DATASET_REGISTRY.datasets.find((d) => d.name === name)
  if (!entry) throw new Error(`unknown dataset "${name}"; known: ${DATASET_NAMES.join(', ')}`)
  return entry
}

/** Entries whose intents include `intent`, in registry order. */
export function datasetsFor(intent: DatasetIntent): DatasetEntry[] {
  return DATASET_REGISTRY.datasets.filter((d) => d.intents.includes(intent))
}
