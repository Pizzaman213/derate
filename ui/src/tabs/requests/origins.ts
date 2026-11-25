// The request map's pure parts: place names, marker size, the view that fits
// the points, the sentences above the map, and the tile URL. React- and
// Leaflet-free, because origins.check.mjs bundles it `platform: 'neutral'`.

import type { GeoDatabaseStatus, GeoLocation, GeoSource, KnownPlace, RouteHop } from '../../api/types'

interface Place {
  city: string | null
  region: string | null
  country: string | null
  label?: string | null
}

/** The operator's name for a known place, else "City, Region, CC"; parts
 *  that are missing are left out. */
export function placeLabel(p: Place): string {
  if (p.label && p.label.trim() !== '') return p.label
  const parts = [p.city, p.region, p.country].filter((s): s is string => !!s && s.trim() !== '')
  return parts.length ? parts.join(', ') : 'unnamed place'
}

/** How far to trust a point: a known place is where the operator said it is;
 *  the IP database, and this coordinator's own address, place a network at
 *  its city's point and no closer; a Tailscale relay is a whole region. */
export function precisionLabel(source: GeoSource): string {
  return source === 'place' ? 'known place' : source === 'relay' ? 'region-level' : 'city-level'
}

/** Street level. The coordinator's tile route caps at the same zoom (tiles.py
 *  MAX_ZOOM) and answers 404 above it. */
export const MAX_TILE_ZOOM = 15

/** The zoom a pin click moves to, centred on the pin: four levels closer, so
 *  three clicks go from the world to the streets (2 -> 6 -> 10 -> 14) and each
 *  one lands near enough to aim the next. Capped at the tiles' own limit. */
export function zoomTowardPin(current: number): number {
  return Math.min(current + 4, MAX_TILE_ZOOM)
}

/** The detail map's zoom for a point from *source*. A known place is where the
 *  operator said it is: zoom 13 is ~15 m/px at mid-latitudes, so a 220 px map
 *  spans ~3 km. A city-level point gets the whole city (zoom 9) and never a
 *  street; a relay region gets the region (zoom 6). */
export function detailZoom(source: GeoSource): number {
  return source === 'place' ? 13 : source === 'relay' ? 6 : 9
}

/** "lat, lon" at the precision the source has: 4 decimals (~11 m) for a known
 *  place, 2 (~1 km) for a city-level point, 1 (~11 km) for a relay region --
 *  more would claim a precision the source does not have. */
export function coordText(lat: number, lon: number, source: GeoSource): string {
  const d = source === 'place' ? 4 : source === 'relay' ? 1 : 2
  return `${lat.toFixed(d)}, ${lon.toFixed(d)}`
}

/** A clicked coordinate as the pin form's field text: 5 decimals (~1 m), finer
 *  than a click at the tile cap can aim. */
export function pickText(v: number): string {
  return String(Math.round(v * 1e5) / 1e5)
}

export interface PinRange {
  value: 'host' | 'block'
  label: string
  cidr: string
}

function parseV4(s: string): number[] | null {
  const m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(s)
  if (!m) return null
  const o = m.slice(1).map(Number)
  return o.every((n) => n <= 255) ? o : null
}

/** Eight 16-bit words, or null. Accepts `::` and a dotted IPv4 tail. */
function parseV6(s: string): number[] | null {
  let head = s.toLowerCase()
  if (head.includes('%')) return null
  let tail: number[] = []
  const dotted = /(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})$/.exec(head)
  if (dotted) {
    const o = parseV4(dotted[1]!)
    if (!o) return null
    tail = [(o[0]! << 8) | o[1]!, (o[2]! << 8) | o[3]!]
    head = head.slice(0, -dotted[1]!.length)
    if (head.endsWith(':') && !head.endsWith('::')) head = head.slice(0, -1)
  }
  const halves = head.split('::')
  if (halves.length > 2) return null
  const left = halves[0] === '' ? [] : halves[0]!.split(':')
  const right = halves.length === 2 && halves[1] !== '' ? halves[1]!.split(':') : []
  if (![...left, ...right].every((h) => /^[0-9a-f]{1,4}$/.test(h))) return null
  const given = left.length + right.length + tail.length
  if (halves.length === 2 ? given > 7 : given !== 8) return null
  return [
    ...left.map((h) => parseInt(h, 16)),
    ...Array<number>(8 - given).fill(0),
    ...right.map((h) => parseInt(h, 16)),
    ...tail,
  ]
}

/** Python's ``ipaddress`` spelling (RFC 5952): lower-case hex without leading
 *  zeros, the first longest run of two or more zero words as `::`. The server
 *  stores ranges in that spelling, so this is what makes a re-pin find its row. */
function v6Text(words: number[]): string {
  let best = -1, bestLen = 0
  for (let i = 0; i < 8; ) {
    if (words[i] !== 0) { i++; continue }
    let j = i
    while (j < 8 && words[j] === 0) j++
    if (j - i > bestLen) { best = i; bestLen = j - i }
    i = j
  }
  const hex = words.map((w) => w.toString(16))
  if (bestLen < 2) return hex.join(':')
  return `${hex.slice(0, best).join(':')}::${hex.slice(best + bestLen).join(':')}`
}

/** The ranges the pin form offers for *ip*: the address itself, or the block
 *  around it (/24 for IPv4, /64 for IPv6), spelled the way the server stores
 *  them. An IPv4-mapped IPv6 address is its IPv4 address -- the server matches
 *  it that way too. Empty when *ip* does not parse. */
export function pinRanges(ip: string): PinRange[] {
  const mapped = /^::ffff:(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})$/i.exec(ip)
  const v4 = parseV4(mapped ? mapped[1]! : ip)
  if (v4) {
    const host = `${v4.join('.')}/32`
    const block = `${v4.slice(0, 3).join('.')}.0/24`
    return [
      { value: 'host', label: `This address (${host})`, cidr: host },
      { value: 'block', label: `Its /24 (${block})`, cidr: block },
    ]
  }
  const v6 = parseV6(ip)
  if (!v6) return []
  const host = `${v6Text(v6)}/128`
  const block = `${v6Text([...v6.slice(0, 4), 0, 0, 0, 0])}/64`
  return [
    { value: 'host', label: `This address (${host})`, cidr: host },
    { value: 'block', label: `Its /64 (${block})`, cidr: block },
  ]
}

/** The table with *place* in it: replacing the row for the same range, in
 *  place, else appended. A re-pin moves the pin rather than being refused as a
 *  duplicate. */
export function withPlace(places: readonly KnownPlace[], place: KnownPlace): KnownPlace[] {
  return places.some((p) => p.cidr === place.cidr)
    ? places.map((p) => (p.cidr === place.cidr ? place : p))
    : [...places, place]
}

/** Area grows with requests: the radius is the square root of the share. */
export function markerRadius(requests: number, max: number): number {
  if (max <= 0) return 4
  return 4 + 14 * Math.sqrt(requests / max)
}

type Bounds = [[number, number], [number, number]]
const clampLat = (v: number) => Math.max(-85, Math.min(85, v))

/** `[[south, west], [north, east]]`, padded; null when there is nothing. */
export function fitBounds(points: readonly { lat: number; lon: number }[]): Bounds | null {
  if (!points.length) return null
  if (points.length === 1) {
    const [{ lat, lon }] = points as [{ lat: number; lon: number }]
    return [[clampLat(lat - 2), lon - 2], [clampLat(lat + 2), lon + 2]]
  }
  let s = Infinity, w = Infinity, n = -Infinity, e = -Infinity
  for (const p of points) {
    s = Math.min(s, p.lat); n = Math.max(n, p.lat)
    w = Math.min(w, p.lon); e = Math.max(e, p.lon)
  }
  const dy = (n - s) * 0.1, dx = (e - w) * 0.1
  return [[clampLat(s - dy), w - dx], [clampLat(n + dy), e + dx]]
}

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`

/** One sentence, ending in a full stop: the database sentence follows it. */
export function originsSummary(
  r: { located: number; local: number; unlocated: number; unknown: number },
  places: number,
): string {
  const parts: string[] = []
  if (r.located) parts.push(`${plural(r.located, 'request', 'requests')} from ${plural(places, 'place', 'places')}`)
  if (r.local) parts.push(`${r.local} from the local network`)
  if (r.unlocated) parts.push(`${r.unlocated} with no location in the database`)
  if (r.unknown) parts.push(`${r.unknown} with no client address`)
  return parts.length ? `${parts.join(' · ')}.` : 'No requests in this window.'
}

export function databaseSentence(db: GeoDatabaseStatus): string {
  switch (db.state) {
    case 'ready':
      return `Locations from DB-IP City Lite, build ${db.build}.`
    case 'downloading':
      return 'Downloading the IP location database…'
    case 'failed':
      return `The IP location database could not be read: ${db.detail}`
    default:
      if (db.last_error)
        return `The IP location database could not be downloaded: ${db.last_error} Retrying in ${Math.ceil(db.retry_in_s / 60)} min.`
      if (db.pinned) return `No IP location database at ${db.path} (DERATE_GEOIP_DB).`
      return 'No IP location database yet.'
  }
}

/** Leaflet's template. The braces are Leaflet's placeholders and must reach
 *  it literally, so this never goes through an encoder. */
export function tileUrlTemplate(origin: string): string {
  return `${origin}/api/map/tiles/{z}/{x}/{y}`
}

/** The tile layer credit: both licences (ODbL, CC BY 4.0) require it on screen.
 *  OpenStreetMap's guidelines accept "© OpenStreetMap" linked to its copyright
 *  page; DB-IP's wording is its own, kept as the API states it. */
export const MAP_ATTRIBUTION =
  '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> · <a href="https://db-ip.com">IP Geolocation by DB-IP</a>'

/** Who a hop was to the request, as the route table and the map's tooltips
 *  name it. */
export function hopTitle(h: Pick<RouteHop, 'role' | 'name' | 'attempt'>): string {
  if (h.role === 'client') return 'Client'
  if (h.role === 'gateway') return `Gateway ${h.name}`
  return `Attempt ${h.attempt} · ${h.name}`
}

export interface RoutePoint extends GeoLocation {
  requests: number
  ips: number
  last_ts: number
  tooltip: string
}

/** A request's route on the map: one point per place a hop sits -- hops at
 *  one place share it, every title in its tooltip -- and the line through
 *  them in the order the request met them. A hop nothing places is left out
 *  of both; the route table still lists it. */
export function routeView(hops: readonly RouteHop[]): { points: RoutePoint[]; path: [number, number][] } {
  const points: RoutePoint[] = []
  const titles: string[][] = []
  const path: [number, number][] = []
  for (const h of hops) {
    const loc = h.location
    if (!loc) continue
    let i = points.findIndex((p) => p.lat === loc.lat && p.lon === loc.lon)
    if (i < 0) {
      i = points.push({ ...loc, requests: 1, ips: 1, last_ts: 0, tooltip: '' }) - 1
      titles.push([])
    }
    titles[i]!.push(hopTitle(h))
    const last = path[path.length - 1]
    if (!last || last[0] !== loc.lat || last[1] !== loc.lon) path.push([loc.lat, loc.lon])
  }
  points.forEach((p, i) => {
    p.tooltip = `${titles[i]!.join(', ')} — ${placeLabel(p)}`
  })
  return { points, path }
}
