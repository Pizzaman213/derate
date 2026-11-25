import { useEffect, useRef, useState } from 'react'
import type { Map as LMap, LayerGroup } from 'leaflet'
import type { OriginPoint } from '../api/types'
import { coordinatorBase } from '../api/origin'
import { MAX_TILE_ZOOM, fitBounds, markerRadius, placeLabel, tileUrlTemplate } from '../tabs/requests/origins'
import { Verbatim } from './Verbatim'

type Leaflet = typeof import('leaflet')

export interface RequestMapProps {
  title: string
  height: number
  /** `tooltip` replaces the "place · N requests · M addresses" line. */
  points: readonly (OriginPoint & { tooltip?: string })[]
  /** Set: a dashed line through these points, in order, under the markers. */
  path?: readonly [number, number][]
  /** The closest zoom a view fitted to the points may reach. 6 by default: a
   *  world of origins. A route inside one city wants street level. */
  fitMaxZoom?: number
  focus?: { lat: number; lon: number; zoom: number }
  attribution: string
  /** Set: a click on the map reports its point, longitude wrapped into
   *  -180..180, and the zoom it was made at. The map itself does not move;
   *  a caller that wants to follow the pick moves `focus`. */
  onPick?: (lat: number, lon: number, zoom: number) => void
  /** Set: a dashed ring of fixed pixel size around this point, saying the
   *  position is approximate. Not a distance: nothing reports an accuracy radius. */
  ring?: { lat: number; lon: number }
  /** Set: a km/mi scale bar, bottom left. For a map zoomed to a place; on a
   *  world view Mercator makes one bar meaningless. */
  scale?: boolean
}

/** Where requests came from, on OpenStreetMap. Tiles come from the
 *  coordinator's /api/map/tiles, never a third-party host. */
export function RequestMap({ title, height, points, focus, attribution, onPick, path, ring, scale, fitMaxZoom = 6 }: RequestMapProps) {
  const host = useRef<HTMLDivElement>(null)
  const map = useRef<{ L: Leaflet; map: LMap; markers: LayerGroup } | null>(null)
  const [ready, setReady] = useState(false)
  const [failed, setFailed] = useState<string | null>(null)
  // Every pane is mounted and hidden when inactive, so Leaflet can be built
  // inside a 0x0 box and would fit its view to that: zoom 1, the world below
  // the frame, every marker `M0 0`. Leaflet re-measures only on a window
  // resize, so the container is watched and the view waits for a real size.
  const [sized, setSized] = useState(false)
  const observer = useRef<ResizeObserver | null>(null)
  // The view last applied. Re-showing a pane with the same view keeps the
  // reader's pan and zoom; only a new view moves the map.
  const appliedView = useRef<string | null>(null)
  // Read at click time, so a new callback does not rebuild the map.
  const pick = useRef(onPick)
  pick.current = onPick

  useEffect(() => {
    let live = true
    const el = host.current
    // A plain wheel over the map scrolls the PAGE. The map sits mid-page above
    // the request list, and a wheel that zoomed it instead trapped the reader
    // on the map and dove into tiles nobody asked for -- blank, and nearly
    // black under the dark theme. Zoom is a pinch (the browser sends it as a
    // ctrl-wheel), Ctrl/Cmd + wheel, a double click or the +/- control.
    // Capture phase on the container, so Leaflet's own wheel listener on the
    // same element never sees a plain one.
    const passWheel = (e: WheelEvent) => {
      if (!e.ctrlKey && !e.metaKey) e.stopImmediatePropagation()
    }
    el?.addEventListener('wheel', passWheel, { capture: true })
    import('./leafletLoader')
      .then(({ default: L }) => {
        if (!live || !host.current) return
        const m = L.map(host.current, { minZoom: 1, maxZoom: MAX_TILE_ZOOM, worldCopyJump: true, attributionControl: true, zoomControl: true })
        // No prefix: Leaflet's licence asks for no on-screen credit, and its
        // default inlines an SVG flag with literal colours. Only the tile and
        // location credits the ODbL and CC BY 4.0 require stay, on one line.
        m.attributionControl.setPrefix(false)
        if (scale) L.control.scale({ position: 'bottomleft', maxWidth: 100 }).addTo(m)
        L.tileLayer(tileUrlTemplate(coordinatorBase()), { maxZoom: MAX_TILE_ZOOM, attribution }).addTo(m)
        m.on('click', (e) => {
          const at = e.latlng.wrap()
          pick.current?.(at.lat, at.lng, m.getZoom())
        })
        map.current = { L, map: m, markers: L.layerGroup().addTo(m) }
        const ro = new ResizeObserver((entries) => {
          const box = entries[entries.length - 1]?.contentRect
          const has = !!box && box.width > 0 && box.height > 0
          // Panning keeps the centre where it was. A window resize while the
          // pane is hidden has Leaflet measure 0x0 itself; without the pan the
          // view would come back anchored at its top-left, off by half a frame.
          if (has) m.invalidateSize()
          setSized(has)
        })
        ro.observe(host.current)
        observer.current = ro
        setReady(true)
      })
      .catch((err: unknown) => {
        if (live) setFailed(err instanceof Error ? err.message : String(err))
      })
    return () => {
      live = false
      el?.removeEventListener('wheel', passWheel, { capture: true })
      observer.current?.disconnect()
      observer.current = null
      map.current?.map.remove()
      map.current = null
      appliedView.current = null
      setReady(false)
      setSized(false)
    }
  }, [attribution, scale])

  const pointsKey = JSON.stringify([points, path, ring])
  useEffect(() => {
    const cur = map.current
    if (!ready || !cur) return
    const { L, markers } = cur
    markers.clearLayers()
    if (path && path.length > 1) {
      L.polyline([...path], { className: 'req-map-route', weight: 2, interactive: false }).addTo(markers)
    }
    if (ring) {
      L.circleMarker([ring.lat, ring.lon], { radius: 42, className: 'req-map-ring', weight: 1.5, interactive: false }).addTo(markers)
    }
    const max = points.reduce((a, p) => Math.max(a, p.requests), 0)
    for (const p of points) {
      L.circleMarker([p.lat, p.lon], { radius: markerRadius(p.requests, max), className: 'req-map-marker', weight: 1.5 })
        .bindTooltip(p.tooltip ?? `${placeLabel(p)} · ${p.requests} requests · ${p.ips} addresses`)
        .addTo(markers)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, pointsKey])

  // The view follows `focus` when there is one, else the points' bounds: a
  // marker that moves under a fixed focus (a pick) leaves the view alone.
  const viewKey = JSON.stringify([focus ?? fitBounds(points), fitMaxZoom])
  useEffect(() => {
    const cur = map.current
    if (!ready || !cur || !sized || appliedView.current === viewKey) return
    const b = fitBounds(points)
    if (focus) cur.map.setView([focus.lat, focus.lon], focus.zoom)
    // Padded so no marker sits on the frame's edge, half off the map.
    else if (b) cur.map.fitBounds(b, { maxZoom: fitMaxZoom, padding: [28, 28] })
    else cur.map.setView([20, 0], 1)
    appliedView.current = viewKey
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, viewKey, sized])

  return (
    <div ref={host} className="req-map" role="img" aria-label={title} aria-busy={ready || failed ? undefined : true} style={{ height }}>
      {failed ? <Verbatim text={failed} /> : null}
    </div>
  )
}
