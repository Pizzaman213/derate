// Bundled base maps and region lookup. Everything is local (JSON imports, no network):
// world-atlas 110m countries and us-atlas 10m states, converted to GeoJSON once and
// memoised. Region keys resolve from any spelling people actually have in a spreadsheet:
// countries by ISO 3166-1 alpha-3, alpha-2, numeric or name (case- and accent-insensitive,
// common aliases included); US states by FIPS, USPS postal code or name.

import { feature } from 'topojson-client'
import type { GeometryCollection, Topology } from 'topojson-specification'
import type { Feature, FeatureCollection, Geometry } from 'geojson'
import worldTopo from './world-110m.topo.json'
import usTopo from './us-states-10m.topo.json'
import countriesTable from './countries.json'
import statesTable from './us-states.json'
import hexTable from './us-hex.json'

export interface Country {
  numeric: string | null
  iso2: string | null
  iso3: string | null
  /** The name used in the topology (Natural Earth short form, e.g. "Dem. Rep. Congo"). */
  name: string
  aliases?: string[]
}
export interface UsState {
  fips: string
  postal: string
  name: string
  aliases?: string[]
}
export interface RegionProps {
  /** Canonical key: ISO3 for countries (the name where none exists), postal code for states. */
  key: string
  name: string
}
export type RegionFeature = Feature<Geometry, RegionProps>
export type MapName = 'world' | 'us-states'
export interface Region {
  key: string
  name: string
  feature: RegionFeature
}

export const COUNTRIES: readonly Country[] = countriesTable
export const US_STATES: readonly UsState[] = statesTable
/** Standard US hex-tile grid: postal code -> { col, row }; odd rows are offset half a tile right. */
export const US_HEX: Readonly<Record<string, { col: number; row: number }>> = hexTable

/** Lower-case, accents and punctuation stripped, whitespace collapsed. */
export function normalizeKey(s: string): string {
  return s
    .normalize('NFD')
    .replace(/\p{M}/gu, '')
    .toLowerCase()
    .replace(/&/g, ' and ')
    .replace(/[.'’ʻ`,()]/g, '')
    .replace(/[\s\-_]+/g, ' ')
    .trim()
}

const countryKey = (c: Country) => c.iso3 ?? c.name
const byName = <T extends { name: string; aliases?: string[] }>(rows: readonly T[]) => {
  const m = new Map<string, T>()
  for (const r of rows) for (const n of [r.name, ...(r.aliases ?? [])]) m.set(normalizeKey(n), r)
  return m
}

const countryIndex = (() => {
  let idx: { numeric: Map<string, Country>; iso2: Map<string, Country>; iso3: Map<string, Country>; name: Map<string, Country> } | null = null
  return () => {
    if (idx) return idx
    idx = { numeric: new Map(), iso2: new Map(), iso3: new Map(), name: byName(COUNTRIES) }
    for (const c of COUNTRIES) {
      if (c.numeric) idx.numeric.set(c.numeric, c)
      if (c.iso2) idx.iso2.set(c.iso2, c)
      if (c.iso3) idx.iso3.set(c.iso3, c)
    }
    return idx
  }
})()

const stateIndex = (() => {
  let idx: { fips: Map<string, UsState>; postal: Map<string, UsState>; name: Map<string, UsState> } | null = null
  return () => {
    if (idx) return idx
    idx = { fips: new Map(US_STATES.map((s) => [s.fips, s])), postal: new Map(US_STATES.map((s) => [s.postal, s])), name: byName(US_STATES) }
    return idx
  }
})()

/** True when the key is a code (numeric, ISO2/3, FIPS, postal) rather than a name. */
export function isCodeKey(key: string): boolean {
  const t = key.trim()
  return /^\d{1,3}$/.test(t) || /^[A-Za-z]{2,3}$/.test(t)
}

/** A country from ISO3, ISO2, ISO numeric (with or without leading zeros) or name. */
export function findCountry(key: string | number): Country | null {
  const idx = countryIndex()
  const t = String(key).trim()
  if (!t) return null
  if (/^\d{1,3}$/.test(t)) return idx.numeric.get(t.padStart(3, '0')) ?? null
  const up = t.toUpperCase()
  if (up.length === 2 && idx.iso2.has(up)) return idx.iso2.get(up)!
  if (up.length === 3 && idx.iso3.has(up)) return idx.iso3.get(up)!
  return idx.name.get(normalizeKey(t)) ?? null
}

/** A US state (or territory) from FIPS (with or without the leading zero), postal code or name. */
export function findState(key: string | number): UsState | null {
  const idx = stateIndex()
  const t = String(key).trim()
  if (!t) return null
  if (/^\d{1,2}$/.test(t)) return idx.fips.get(t.padStart(2, '0')) ?? null
  const up = t.toUpperCase()
  if (up.length === 2 && idx.postal.has(up)) return idx.postal.get(up)!
  return idx.name.get(normalizeKey(t)) ?? null
}

type CountryTopo = Topology<{ countries: GeometryCollection<{ name: string }>; land: GeometryCollection }>
type StatesTopo = Topology<{ states: GeometryCollection<{ name: string }>; nation: GeometryCollection }>

const memo = <T>(make: () => T) => {
  let v: T | undefined
  return () => (v ??= make())
}

/** Every country in world-110m as GeoJSON features; `properties.key` is the canonical key. */
export const worldCountries = memo((): RegionFeature[] => {
  const topo = worldTopo as unknown as CountryTopo
  const fc = feature(topo, topo.objects.countries) as FeatureCollection<Geometry, { name: string }>
  return fc.features.map((f) => {
    const c = (f.id !== undefined ? findCountry(String(f.id)) : null) ?? findCountry(f.properties.name)
    const key = c ? countryKey(c) : f.properties.name
    return { type: 'Feature', id: key, geometry: f.geometry, properties: { key, name: c?.name ?? f.properties.name } }
  })
})

/** Every state, DC and territory in us-states-10m; `properties.key` is the postal code. */
export const usStates = memo((): RegionFeature[] => {
  const topo = usTopo as unknown as StatesTopo
  const fc = feature(topo, topo.objects.states) as FeatureCollection<Geometry, { name: string }>
  return fc.features.map((f) => {
    const s = findState(String(f.id ?? '')) ?? findState(f.properties.name)
    const key = s?.postal ?? f.properties.name
    return { type: 'Feature', id: key, geometry: f.geometry, properties: { key, name: s?.name ?? f.properties.name } }
  })
})

/** The features of a base map. */
export function mapFeatures(map: MapName): RegionFeature[] {
  return map === 'us-states' ? usStates() : worldCountries()
}

const worldIndex = memo(() => new Map(worldCountries().map((f) => [f.properties.key, f])))
const usIndex = memo(() => new Map(usStates().map((f) => [f.properties.key, f])))

/** Resolve a region key on a base map to its canonical key, name and feature, or null. */
export function resolveRegion(map: MapName, key: string | number): Region | null {
  if (map === 'us-states') {
    const s = findState(key)
    const f = s ? usIndex().get(s.postal) : undefined
    return s && f ? { key: s.postal, name: s.name, feature: f } : null
  }
  const c = findCountry(key)
  const f = c ? worldIndex().get(countryKey(c)) : undefined
  return c && f ? { key: countryKey(c), name: c.name, feature: f } : null
}

/** A feature by key on either map (any accepted spelling). */
export function countryFeature(key: string | number): RegionFeature | null {
  return resolveRegion('world', key)?.feature ?? null
}
export function stateFeature(key: string | number): RegionFeature | null {
  return resolveRegion('us-states', key)?.feature ?? null
}
