// What the data looks like: its kind (rows, tree, graph, sets, KPI, matrix, numbers,
// text), and for rows every column's type, cardinality, nulls, sign and order, plus the
// structures the chooser keys on (time, intervals, hierarchy, links, geo, OHLC, sets).

import { toRows } from '../adapters/rows'
import { parseDate, isDateLike } from '../time/parse'
import { findCountry, findState } from '../geo/index'

export type ColumnType = 'number' | 'date' | 'category' | 'boolean' | 'empty'
export interface Column {
  name: string
  type: ColumnType
  distinct: number
  nulls: number
  negatives: number
  /** Values already in ascending order (dates/numbers), ignoring nulls. */
  ascending: boolean
  /** Longest label in characters (categories). */
  maxLen: number
  /** Every row has a different value. */
  unique: boolean
}
export type DataKind = 'rows' | 'tree' | 'graph' | 'sets' | 'kpi' | 'matrix' | 'numbers' | 'text' | 'empty' | 'unknown'
export interface DataShape {
  kind: DataKind
  rows: Record<string, unknown>[]
  count: number
  columns: Column[]
  numeric: Column[]
  temporal: Column[]
  categorical: Column[]
  /** start/end interval columns (timelines). */
  interval: { start: string; end: string | null; duration: string | null } | null
  /** id/parent or path columns (rows that form a tree). */
  hierarchy: { id: string; parent: string } | { path: string } | null
  /** source/target columns (rows that form a graph). */
  links: { source: string; target: string; value: string | null } | null
  geo: { region: string | null; map: 'world' | 'us-states' | null; lat: string | null; lon: string | null }
  ohlc: { date: string; open: string; high: string; low: string; close: string } | null
  sets: string | null
  /** The raw object for non-row kinds. */
  raw: unknown
}

const NAME = (re: RegExp) => (c: Column) => re.test(c.name.toLowerCase())
const by = (cols: Column[], re: RegExp) => cols.find(NAME(re))

function columnOf(name: string, rows: Record<string, unknown>[]): Column {
  let num = 0, date = 0, bool = 0, str = 0, nulls = 0, negatives = 0, maxLen = 0
  const seen = new Set<string>()
  let asc = true, prev = -Infinity
  for (const r of rows) {
    const v = r[name]
    if (v === null || v === undefined || v === '' || (typeof v === 'number' && Number.isNaN(v))) { nulls++; continue }
    seen.add(String(v))
    if (typeof v === 'boolean') { bool++; continue }
    if (typeof v === 'number') {
      num++
      if (v < 0) negatives++
      if (v < prev) asc = false
      prev = v
      continue
    }
    if (v instanceof Date || (typeof v === 'string' && isDateLike(v))) {
      date++
      const t = +(v instanceof Date ? v : parseDate(v) ?? NaN)
      if (t < prev) asc = false
      prev = t
      continue
    }
    if (typeof v === 'string') {
      const n = Number(v.replace(/,/g, ''))
      if (v.trim() !== '' && Number.isFinite(n) && /^-?[\d,.]+(e-?\d+)?$/i.test(v.trim())) {
        num++
        if (n < 0) negatives++
        continue
      }
      str++
      maxLen = Math.max(maxLen, v.length)
    }
  }
  const filled = rows.length - nulls
  const type: ColumnType = filled === 0 ? 'empty' : num === filled ? 'number' : date === filled ? 'date' : bool === filled ? 'boolean' : 'category'
  if (type === 'category') for (const r of rows) maxLen = Math.max(maxLen, String(r[name] ?? '').length)
  return { name, type, distinct: seen.size, nulls, negatives, ascending: asc, maxLen, unique: seen.size === filled && filled === rows.length }
}

const isObj = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v)

/** Describe the data so the chooser can match charts to it. */
export function inspect(data: unknown): DataShape {
  const base: DataShape = { kind: 'unknown', rows: [], count: 0, columns: [], numeric: [], temporal: [], categorical: [], interval: null, hierarchy: null, links: null, geo: { region: null, map: null, lat: null, lon: null }, ohlc: null, sets: null, raw: data }
  if (data === null || data === undefined) return { ...base, kind: 'empty' }
  if (Array.isArray(data) && data.length === 0) return { ...base, kind: 'empty' }
  if (Array.isArray(data) && data.every((v) => typeof v === 'number' || v === null)) return { ...base, kind: 'numbers', count: data.length }
  if (typeof data === 'string' && !/[\n\r]/.test(data.trim())) return { ...base, kind: 'text', count: data.length }
  if (typeof data === 'string' && !data.split(/\r?\n/)[0]!.match(/[,;\t|]/)) return { ...base, kind: 'text', count: data.length }
  if (isObj(data)) {
    if (Array.isArray(data.children) || ('name' in data && 'children' in data)) return { ...base, kind: 'tree' }
    if (Array.isArray(data.links)) return { ...base, kind: 'graph', count: data.links.length }
    if (Array.isArray(data.sets)) return { ...base, kind: 'sets', count: data.sets.length }
    if (Array.isArray(data.matrix) && Array.isArray(data.names)) return { ...base, kind: 'matrix', count: data.names.length }
    if (typeof data.value === 'number' || (typeof data.value === 'string' && data.value.trim() !== '' && Number.isFinite(Number(data.value)))) return { ...base, kind: 'kpi', count: 1 }
  }
  let rows: Record<string, unknown>[] | null = null
  try { rows = toRows(data) } catch { rows = null }
  if (!rows) return base
  if (!rows.length) return { ...base, kind: 'empty' }
  const names = [...new Set(rows.slice(0, 2000).flatMap((r) => Object.keys(r)))]
  const sample = rows.length > 5000 ? rows.filter((_, i) => i % Math.ceil(rows!.length / 5000) === 0) : rows
  const columns = names.map((n) => columnOf(n, sample))
  const numeric = columns.filter((c) => c.type === 'number')
  const temporal = columns.filter((c) => c.type === 'date')
  const categorical = columns.filter((c) => c.type === 'category')
  const shape: DataShape = { ...base, kind: 'rows', rows, count: rows.length, columns, numeric, temporal, categorical }
  // intervals
  const start = by(temporal, /^(start|begin|from|start_?date|starts?_at)$/)
  if (start) shape.interval = { start: start.name, end: by(temporal, /^(end|finish|to|due|end_?date|ends?_at)$/)?.name ?? null, duration: by(numeric, /^(duration|days|length)$/)?.name ?? null }
  // hierarchy
  const id = by(columns, /^(id|key|node|code)$/), parent = by(columns, /^(parent|parent_?id|manager|reports_?to|parentid)$/)
  if (id && parent) shape.hierarchy = { id: id.name, parent: parent.name }
  else { const path = categorical.find((c) => /path/.test(c.name.toLowerCase()) && rows!.some((r) => String(r[c.name] ?? '').includes('/'))); if (path) shape.hierarchy = { path: path.name } }
  // links
  const src = by(categorical, /^(source|from|src|origin)$/), tgt = by(categorical, /^(target|to|dst|destination)$/)
  if (src && tgt) shape.links = { source: src.name, target: tgt.name, value: by(numeric, /^(value|weight|count|amount|flow|volume)$/)?.name ?? null }
  // geo
  const lat = by(numeric, /^(lat|latitude)$/), lon = by(numeric, /^(lon|lng|long|longitude)$/)
  shape.geo.lat = lat?.name ?? null
  shape.geo.lon = lon?.name ?? null
  let best: { col: string; map: 'world' | 'us-states'; hit: number } | null = null
  for (const c of categorical) {
    const vals = [...new Set(rows.slice(0, 400).map((r) => String(r[c.name] ?? '')))].filter(Boolean)
    if (!vals.length) continue
    const states = vals.filter((v) => findState(v)).length / vals.length
    const countries = vals.filter((v) => findCountry(v)).length / vals.length
    for (const [map, hit] of [['us-states', states], ['world', countries]] as const)
      if (hit >= 0.7 && (!best || hit > best.hit || (hit === best.hit && /iso|country|state|region/.test(c.name)))) best = { col: c.name, map, hit }
  }
  if (best) { shape.geo.region = best.col; shape.geo.map = best.map }
  // OHLC
  const o = by(numeric, /^open$/), hi = by(numeric, /^high$/), lo = by(numeric, /^low$/), cl = by(numeric, /^close$/)
  if (o && hi && lo && cl && temporal[0]) shape.ohlc = { date: temporal[0].name, open: o.name, high: hi.name, low: lo.name, close: cl.name }
  // set memberships
  const sets = columns.find((c) => /^sets?$/.test(c.name.toLowerCase()))
  if (sets) shape.sets = sets.name
  return shape
}
