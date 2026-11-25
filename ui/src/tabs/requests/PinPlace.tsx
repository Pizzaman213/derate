import { useState } from 'react'
import type { GeoLocation, OriginPoint } from '../../api/types'
import { RequestMap } from '../../components/RequestMap'
import { Verbatim } from '../../components/Verbatim'
import { useBackend } from '../../state/backend'
import { useGeoipPlaces } from '../../state/resources'
import { MAP_ATTRIBUTION, detailZoom, pickText, pinRanges, withPlace, zoomTowardPin } from './origins'

/** A request's address pinned to a known place, from the request itself: the
 *  range is the address or its block, the point is a click on the map (or
 *  typed). Saves through the same `PUT /api/geoip/places` as Settings > Policy
 *  > Known places, replacing a row for the same range, so a second pin moves
 *  the first. Coordinates start empty unless the address already has a known
 *  place: copying the database's city point in would save a city-level guess
 *  as if somebody knew it. */
export function PinPlace({ ip, loc, onDone }: { ip: string; loc: GeoLocation | null; onDone: () => void }) {
  const places = useGeoipPlaces()
  const { backend, invalidate } = useBackend()
  const ranges = pinRanges(ip)
  const known = loc?.source === 'place' ? loc : null

  const [range, setRange] = useState<'host' | 'block'>('host')
  const [label, setLabel] = useState(known?.label ?? '')
  const [lat, setLat] = useState(known ? String(known.lat) : '')
  const [lon, setLon] = useState(known ? String(known.lon) : '')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  // The view follows the pin: a click centres on it four levels closer, so a
  // pin dropped on the whole world is three clicks from the right street, and
  // typed coordinates are shown at street level once both fields are numbers.
  const [focus, setFocus] = useState(() =>
    loc ? { lat: loc.lat, lon: loc.lon, zoom: detailZoom(loc.source) } : { lat: 20, lon: 0, zoom: 2 },
  )

  // What the map shows is what Save sends: the typed or clicked point when
  // both fields are numbers, else where the address resolves now.
  const la = Number(lat.trim())
  const lo = Number(lon.trim())
  const set = lat.trim() !== '' && lon.trim() !== '' && Number.isFinite(la) && Number.isFinite(lo)
  const base = { city: null, region: null, country: null, requests: 1, ips: 1, last_ts: 0 }
  const points: OriginPoint[] = set
    ? [{ ...base, lat: la, lon: lo, label: label.trim() || 'New pin', source: 'place' }]
    : loc
      ? [{ ...base, ...loc }]
      : []
  // Typed coordinates are precise by intent: centre on them at street level,
  // or closer if the view already is. On blur, so the map does not chase a
  // half-typed number.
  const showTyped = () => {
    if (set) setFocus({ lat: la, lon: lo, zoom: Math.max(focus.zoom, detailZoom('place')) })
  }

  const save = async () => {
    const cidr = ranges.find((r) => r.value === range)?.cidr
    if (!places.data || !cidr) return
    if (!set) {
      setError('Click the map where this address is, or type a latitude and longitude.')
      return
    }
    setError(null)
    setBusy(true)
    try {
      await backend.putGeoipPlaces(withPlace(places.data.places, { cidr, label: label.trim(), lat: la, lon: lo }))
      invalidate()
      onDone()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div style={{ marginTop: 'var(--space-12)' }}>
      <h4 className="label">Pin {ip}</h4>
      <div className="unit">
        {loc
          ? 'Click the map where this address really is, or type the coordinates.'
          : 'Nothing places this address from its IP: a LAN or tailnet address has no public location. Click the map where it really is, or type the coordinates.'}{' '}
        A pin beats the IP database for every address in its range.
      </div>
      {places.error ? <Verbatim text={places.error.message} size="unit" /> : null}
      <div style={{ marginTop: 'var(--space-8)' }}>
        <RequestMap
          title={`Where to pin ${ip}`}
          height={260}
          points={points}
          focus={focus}
          attribution={MAP_ATTRIBUTION}
          onPick={(a, b, zoom) => {
            setLat(pickText(a))
            setLon(pickText(b))
            setFocus({ lat: a, lon: b, zoom: zoomTowardPin(zoom) })
          }}
        />
      </div>
      <div
        style={{
          display: 'flex',
          gap: 'var(--space-10)',
          alignItems: 'flex-end',
          flexWrap: 'wrap',
          marginTop: 'var(--space-10)',
        }}
      >
        {/* Native, like every other `.fld` row's select (KeyCreate, LimitsEditor):
            `.fld select` shares the inputs' padding, so the row stays on one line. */}
        <div className="fld">
          <label htmlFor="pin-range">Range</label>
          <select id="pin-range" value={range} onChange={(e) => setRange(e.target.value as 'host' | 'block')}>
            {ranges.map((r) => (
              <option key={r.value} value={r.value}>
                {r.label}
              </option>
            ))}
          </select>
        </div>
        <div className="fld" style={{ width: 160 }}>
          <label htmlFor="pin-label">Label</label>
          <input id="pin-label" value={label} autoComplete="off" onChange={(e) => setLabel(e.target.value)} />
        </div>
        <div className="fld" style={{ width: 110 }}>
          <label htmlFor="pin-lat">Latitude</label>
          <input
            id="pin-lat"
            className="mono"
            inputMode="decimal"
            value={lat}
            autoComplete="off"
            onChange={(e) => setLat(e.target.value)}
            onBlur={showTyped}
          />
        </div>
        <div className="fld" style={{ width: 110 }}>
          <label htmlFor="pin-lon">Longitude</label>
          <input
            id="pin-lon"
            className="mono"
            inputMode="decimal"
            value={lon}
            autoComplete="off"
            onChange={(e) => setLon(e.target.value)}
            onBlur={showTyped}
          />
        </div>
        <button type="button" disabled={busy || !places.data} onClick={() => void save()}>
          {busy ? 'Saving…' : 'Save pin'}
        </button>
        <button type="button" className="ghost" disabled={busy} onClick={onDone}>
          Cancel
        </button>
      </div>
      {error ? (
        <div className="label" style={{ color: 'var(--fault)', marginTop: 'var(--space-8)', whiteSpace: 'pre-wrap' }}>
          {error}
        </div>
      ) : null}
    </div>
  )
}
