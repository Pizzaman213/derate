import { useState } from 'react'
import type { KnownPlace } from '../../api/types'
import { Verbatim } from '../../components/Verbatim'
import { useBackend } from '../../state/backend'
import { useGeoipPlaces } from '../../state/resources'

/** The operator's CIDR -> place table (`/api/geoip/places`). IP geolocation
 *  stops at the city; a range somebody names here is exact, beats the
 *  database, and is the only way a loopback or LAN address reaches the map.
 *
 *  Every save sends the whole table -- the route replaces it -- so add and
 *  remove are the same PUT. The server's refusal names the row and field
 *  (`places[1].cidr: ...`) and is shown as it came. */
export function PlacesCard() {
  const places = useGeoipPlaces()
  const { backend, invalidate } = useBackend()
  const data = places.data

  const [cidr, setCidr] = useState('')
  const [label, setLabel] = useState('')
  const [lat, setLat] = useState('')
  const [lon, setLon] = useState('')
  const [error, setError] = useState<string | null>(null)

  const put = async (next: KnownPlace[]): Promise<boolean> => {
    setError(null)
    try {
      await backend.putGeoipPlaces(next)
      invalidate()
      return true
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      return false
    }
  }

  const add = async () => {
    if (!data) return
    const la = Number(lat.trim())
    const lo = Number(lon.trim())
    if (lat.trim() === '' || lon.trim() === '' || !Number.isFinite(la) || !Number.isFinite(lo)) {
      setError('Latitude and longitude must be numbers.')
      return
    }
    if (await put([...data.places, { cidr, label, lat: la, lon: lo }])) {
      setCidr('')
      setLabel('')
      setLat('')
      setLon('')
    }
  }

  const remove = async (i: number) => {
    if (!data) return
    await put(data.places.filter((_, j) => j !== i))
  }

  return (
    <div className="card2">
      <h3>Known places</h3>
      <div className="unit">
        Addresses you can place yourself: your own networks, an office&apos;s egress address, a VPN. A match
        here beats the IP database and is the only way the local network appears on the request map.
      </div>
      {places.error ? <Verbatim text={places.error.message} size="unit" /> : null}
      {data?.error ? <Verbatim text={data.error} size="unit" /> : null}
      {!data ? (
        places.error ? null : (
          <div className="unit" aria-busy="true" aria-live="polite">
            Reading known places…
          </div>
        )
      ) : data.places.length === 0 ? (
        <div className="unit">No places yet.</div>
      ) : (
        <div style={{ overflowX: 'auto', marginTop: 'var(--space-10)' }}>
          <table>
            <thead>
              <tr>
                <th scope="col">Range</th>
                <th scope="col">Label</th>
                <th scope="col" style={{ textAlign: 'right' }}>
                  Latitude
                </th>
                <th scope="col" style={{ textAlign: 'right' }}>
                  Longitude
                </th>
                <th scope="col">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {data.places.map((p, i) => (
                <tr key={p.cidr}>
                  <td className="mono">{p.cidr}</td>
                  <td>{p.label}</td>
                  <td className="num mono">{p.lat}</td>
                  <td className="num mono">{p.lon}</td>
                  <td style={{ textAlign: 'right' }}>
                    <button type="button" className="ghost" onClick={() => void remove(i)}>
                      Remove
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div
        style={{
          display: 'flex',
          gap: 'var(--space-10)',
          alignItems: 'flex-end',
          flexWrap: 'wrap',
          marginTop: 'var(--space-10)',
        }}
      >
        <div className="fld" style={{ width: 170 }}>
          <label htmlFor="place-cidr">Range</label>
          <input
            id="place-cidr"
            className="mono"
            value={cidr}
            placeholder="192.168.0.0/24"
            autoComplete="off"
            onChange={(e) => setCidr(e.target.value)}
          />
        </div>
        <div className="fld" style={{ width: 160 }}>
          <label htmlFor="place-label">Label</label>
          <input id="place-label" value={label} autoComplete="off" onChange={(e) => setLabel(e.target.value)} />
        </div>
        <div className="fld" style={{ width: 100 }}>
          <label htmlFor="place-lat">Latitude</label>
          <input
            id="place-lat"
            className="mono"
            inputMode="decimal"
            value={lat}
            autoComplete="off"
            onChange={(e) => setLat(e.target.value)}
          />
        </div>
        <div className="fld" style={{ width: 100 }}>
          <label htmlFor="place-lon">Longitude</label>
          <input
            id="place-lon"
            className="mono"
            inputMode="decimal"
            value={lon}
            autoComplete="off"
            onChange={(e) => setLon(e.target.value)}
          />
        </div>
        <button type="button" disabled={!data} onClick={() => void add()}>
          Add
        </button>
      </div>

      {error ? (
        <div className="label" style={{ color: 'var(--fault)', marginTop: 'var(--space-8)', whiteSpace: 'pre-wrap' }}>
          {error}
        </div>
      ) : null}
      {data ? (
        <div className="unit" style={{ marginTop: 'var(--space-8)' }}>
          Stored in <span className="mono">{data.path}</span>.
        </div>
      ) : null}
    </div>
  )
}
