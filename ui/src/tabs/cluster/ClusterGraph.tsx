import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type MutableRefObject,
  type RefObject,
} from 'react'
import type {
  DeploymentDTO,
  NodeStateDTO,
  Provider,
  RoutingConfig,
  Topology,
  TopologyNode,
} from '../../api/types'
import { useSelection } from '../../state/selection'
import { useRequestMix } from '../../state/resources'
import { useMetrics } from '../../state/metrics'
import { nodeLive, nodeSignal, utilKind } from '../../state/live'
import { PROPORTIONAL } from '../../state/policy'
import { fmt, pct } from '../../format'
import { nodeName, nodeSubtitle } from '../../state/names'
import type { EndpointPlate } from './layout'
import { ease, lerpRoute, sameShape, shifts } from './motion'
import {
  ENTRY_FONT,
  ENTRY_PAD,
  EXIT_FONT,
  EXIT_PAD,
  PROVIDER_LOGO,
  PROVIDER_LOGO_DY,
  PROVIDER_ROW_H,
  PROVIDER_ROW_TEXT_X,
  PROVIDER_TEXT_X,
  CORNER_R,
  layoutCluster,
  moveToSlot,
  roundedPath,
  outFlightKey,
  PROVIDER_NODE_ID,
  plateWidth,
  bandLabelWidth,
  bandSubline,
  plateOccupant,
  plateSpec,
  runners,
  type ClusterBand,
  type ClusterEdge,
  type ClusterLayout,
  type PlacedCard,
  type Point,
} from './layout'
import {
  mostlyStreaming,
  streamingRatio,
  targetFlows,
  useParticleField,
  type StreamFlow,
  type TargetFlow,
} from './particles'
import { BandLaunchLayer, BandSweep } from './LoadingBand'
import {
  markProviderLogoMissing,
  providerLogoMissing,
  providerLogoUrl,
  providerMonogram,
  subscribeProviderLogos,
} from './providerLogo'

/** Zoom range, RELATIVE TO THE FIT: 0.4x to 4x the framing that fills the
 *  floor. Absolute limits would be useless now that the resting view is itself
 *  a fit -- a fit that landed at 4 would leave no room to zoom in at all. */
const ZOOM_MIN = 0.4
const ZOOM_MAX = 4
/** Authored units of breathing room between the ink and the viewBox edge. */
const FIT_PAD = 12
/** Authored units a pointer must travel before a press on a plate becomes a
 *  drag rather than a click. Without it, selecting a machine nudges it. */
const DRAG_SLOP = 4
/** How long after a press on a plate a second press still reads as a
 *  double-click. The browser's own `dblclick` never reaches a plate: the drag
 *  handler takes pointer capture on the SVG the moment a plate is pressed, and
 *  capture retargets the compatibility mouse events -- `click` and `dblclick`
 *  included -- to the capture element, so a React handler on the plate would
 *  never fire. The press path below therefore pairs the two presses itself. */
const DOUBLE_MS = 400

export interface ClusterGraphHandle {
  reset: () => void
}

interface Props {
  deployments: DeploymentDTO[]
  topology: Topology
  /** Cluster's own node rows: the live telemetry source, and the only place
   *  `healthy` / `state` for the state border comes from. */
  nodes: NodeStateDTO[]
  routing: RoutingConfig[]
  /** Only for the mark and the tooltip on each provider row. Deliberately NOT
   *  passed into `layoutCluster`: the geometry must stay a pure function of the
   *  topology payload, and a 15s-TTL resource in its memo deps would rebuild
   *  every plate on a poll that changes nothing about them. */
  providers: Provider[]
  zoomLabelRef: RefObject<HTMLSpanElement>
  order: string[] | null
  onReorder: (order: string[]) => void
  /** Where each machine has been dragged to, as a displacement from the slot
   *  `order` deals it. See `tabs/cluster/order.ts`. */
  offsets: Record<string, Point>
  /** One machine placed. `null` puts it back on its slot. Called on drop, not
   *  on every pointer move: the ghost is what follows the pointer, and a
   *  localStorage write per frame buys nothing. */
  onMove: (nodeId: string, offset: Point | null) => void
  announce: (message: string) => void
}

/** The machine floor, drawn in the mockup's idiom.
 *
 *  Everything is in AUTHORED units: the viewBox is `layout.width` wide while
 *  the element is 100% wide, and `layout.width` is the container divided by
 *  GSCALE -- so the whole drawing renders 4/3 larger than authored and 9px
 *  type lands on the 12px floor the rest of the panel keeps. Do not mix real
 *  pixels in here.
 *
 *  Two things composite on top of the scene and must NOT force it to
 *  re-render: the pan/zoom transform (a ref-written DOM attribute, never React
 *  state) and the drag ghost. Live telemetry is different -- the meter fill and
 *  the state border are part of the plate, not an overlay on it, so painting
 *  them after the plate's own text would cover it. The plates are therefore one
 *  live component re-rendering at 1 Hz; at twelve machines that is twelve small
 *  groups a second, the same order as the hero readout. */
export const ClusterGraph = forwardRef<ClusterGraphHandle, Props>(function ClusterGraph(
  {
    deployments,
    topology,
    nodes,
    routing,
    providers,
    zoomLabelRef,
    order,
    onReorder,
    offsets,
    onMove,
    announce,
  },
  ref,
) {
  const { selDep, selNode, selLink, selectDep, selectNode, selectLink, openSheet } = useSelection()

  const hostRef = useRef<HTMLDivElement>(null)
  const svgRef = useRef<SVGSVGElement>(null)
  const sceneRef = useRef<SVGGElement>(null)
  const particleLayerRef = useRef<SVGGElement>(null)
  const [box, setBox] = useState({ w: 0, h: 0 })

  // Both axes. The viewBox is the element's own box, so the drawing can be
  // fitted into all of it rather than sitting at its content height in the
  // middle of a tall floor. No feedback loop: .floor is `flex: 1; min-height:
  // 0`, so its height comes from the flex container's free space and never
  // from this element's content.
  useEffect(() => {
    const el = hostRef.current
    if (!el) return
    const read = (w: number, h: number) =>
      setBox((b) => (b.w === Math.floor(w) && b.h === Math.floor(h) ? b : { w: Math.floor(w), h: Math.floor(h) }))
    const ro = new ResizeObserver(([entry]) => {
      if (entry) read(entry.contentRect.width, entry.contentRect.height)
    })
    ro.observe(el)
    const r = el.getBoundingClientRect()
    read(r.width, r.height)
    return () => ro.disconnect()
  }, [])

  // One set for the whole drawing. `layoutCluster` filters its own input the
  // same way, but the plate's served-name list and the launch layer are read
  // straight off this prop -- and a machine captioned with a model that failed
  // to start, under a floor that no longer draws a band for it, is the same
  // ledger showing through in a second place.
  const live = useMemo(() => runners(deployments), [deployments])

  const layout = useMemo(
    () =>
      layoutCluster({
        nodes: topology.nodes,
        links: topology.edges,
        deployments: live,
        // Absent on a coordinator older than this key. [] means "none", which
        // is what an older coordinator's floor has always drawn.
        remotes: topology.remotes ?? [],
        routing,
        selection: { selDep, selNode, selLink },
        width: box.w,
        height: box.h,
        order,
        offsets,
      }),
    [topology, live, routing, selDep, selNode, selLink, box, order, offsets],
  )

  // A plate the pointer just put down is already where the drop left it. It
  // must not then be animated there from the slot it used to occupy.
  const justPlaced = useRef<string | null>(null)
  useFloorMotion(layout, sceneRef, justPlaced)

  const providerById = useMemo(
    () => new Map(providers.map((p) => [p.provider_id, p])),
    [providers],
  )

  // ── The fit: the framing that fills the floor ─────────────────────────────
  //
  // The resting view is not the identity. The viewBox is the element's box, so
  // at k = 1 a two-machine drawing occupies a fifth of it; the fit scales the
  // ink up until it meets the viewBox on whichever axis binds first, and
  // centres it there. A drawing bigger than the floor scales DOWN by the same
  // expression, so the dense case needs no separate branch.
  const fit = useMemo(() => {
    const { ink, width: W, height: H, offsetX } = layout
    // `ink` is pre-offsetX, the space every other layout coordinate is in; the
    // renderer applies the translate, so the framing has to account for it.
    const cx = ink.x + offsetX + ink.w / 2
    const cy = ink.y + ink.h / 2
    const k = Math.max(
      ZOOM_MIN,
      Math.min(ZOOM_MAX, (W - 2 * FIT_PAD) / ink.w, (H - 2 * FIT_PAD) / ink.h),
    )
    return { k, tx: W / 2 - cx * k, ty: H / 2 - cy * k }
  }, [layout])

  // ── Pan and zoom: a ref-written transform, never React state ──────────────
  const view = useRef({ ...fit })
  const drag = useRef<{ x: number; y: number; tx: number; ty: number } | null>(null)
  /** The fit, readable from the pointer/key handlers without re-binding them
   *  every time the floor resizes. */
  const fitRef = useRef(fit)
  fitRef.current = fit
  /** Has anyone panned or zoomed? Until they have, a resize or a change in the
   *  cluster re-frames; after, their framing is theirs and survives both. */
  const userMoved = useRef(false)

  const applyView = () => {
    sceneRef.current?.setAttribute(
      'transform',
      `translate(${view.current.tx.toFixed(1)} ${view.current.ty.toFixed(1)}) scale(${view.current.k.toFixed(3)})`,
    )
    if (zoomLabelRef.current) {
      // Relative to the fit, so the framing everything rests at reads 100% --
      // what the toolbar promises. An absolute figure would read 380% at rest
      // and mean nothing, since the fit moves with the floor.
      zoomLabelRef.current.textContent = `${Math.round((view.current.k / fitRef.current.k) * 100)}%`
    }
  }

  const resetView = () => {
    view.current = { ...fitRef.current }
    userMoved.current = false
    applyView()
  }

  useImperativeHandle(ref, () => ({ reset: resetView }))

  useEffect(() => {
    if (!userMoved.current) view.current = { ...fit }
    applyView()
    // eslint-disable-next-line react-hooks/exhaustive-deps -- applyView only touches refs
  }, [fit])

  // ── Plate drag ────────────────────────────────────────────────────────────
  //
  // A plate goes where it is dropped and stays there. What is persisted is a
  // DISPLACEMENT from the slot the arrangement deals it, never an absolute
  // x/y: an absolute position stops meaning anything the moment the viewport
  // resizes or the cluster crosses a density tier and the plate size changes,
  // while an offset travels with its slot through both.
  //
  // Nothing snaps and nothing is refused. A plate can be dropped on top of a
  // link, or on top of another plate -- the floor is a drawing of a rack
  // somebody is looking at, and where they want a machine is not a question
  // this component gets a vote on. Two things do follow a moved plate, because
  // they are drawings OF it rather than decoration near it: its links re-aim
  // (a bracket where there is a clear channel, an arc where there is not) and
  // its band's bar re-spans, both worked out from the final geometry in
  // layout.ts rather than from the slot it came from.
  const cardDrag = useRef<{
    nodeId: string
    from: Point
    /** Where this plate already was, relative to its slot, when the press
     *  landed. A drag adds to it -- the alternative resets the plate to its
     *  slot the moment a second drag starts. */
    base: Point
    dx: number
    dy: number
    moved: boolean
  } | null>(null)
  /** The last press on a plate that did not become a drag: the first half of a
   *  double-click, waiting to see whether a second one lands on the same
   *  machine inside DOUBLE_MS. */
  const lastPress = useRef<{ nodeId: string; at: number } | null>(null)
  const ghostRef = useRef<SVGGElement>(null)
  const [dragging, setDragging] = useState<string | null>(null)

  // The plates are captioned by name, so the announcements have to be too --
  // a screen reader saying "spark-02 moved" about a plate reading "Rack 2" is
  // the same ambiguity this feature exists to remove, just in another channel.
  // The provider bus has no topology entry to name it from -- PROVIDER_NODE_ID
  // is a synthetic key, never a caption -- so it gets the one it is captioned
  // with on the floor instead of that raw id read aloud. A band's own id is a
  // deployment id or `remote:<name>`, neither of which is ever on screen --
  // the served name is what its caption actually says.
  const name = (nodeId: string) =>
    nodeId === PROVIDER_NODE_ID
      ? 'Providers'
      : layout.bands.find((b) => b.id === nodeId)?.servedName ??
        nodeName(topology.nodes.find((n) => n.node_id === nodeId), nodeId)

  /** Where a plate sits relative to its slot, spoken. The plates are captioned
   *  by name and drawn where somebody put them, so the announcement has to say
   *  both -- "moved" on its own tells a screen reader nothing it can act on. */
  const placement = (o: Point) => {
    const parts = [
      o.x ? `${Math.abs(Math.round(o.x))} ${o.x > 0 ? 'right' : 'left'}` : '',
      o.y ? `${Math.abs(Math.round(o.y))} ${o.y > 0 ? 'down' : 'up'}` : '',
    ].filter(Boolean)
    return parts.length ? `${parts.join(' and ')} of its default position` : 'in its default position'
  }

  const offsetOf = (nodeId: string): Point =>
    layout.cards.find((c) => c.nodeId === nodeId)?.offset ?? { x: 0, y: 0 }

  const commitOrder = (nodeId: string, slot: number, spoken: string) => {
    const next = moveToSlot(layout.arrangement, nodeId, slot)
    const o = offsetOf(nodeId)
    const placed = o.x !== 0 || o.y !== 0
    if (next === layout.arrangement && !placed) return
    if (next !== layout.arrangement) onReorder(next)
    // The keyboard names a SLOT, so the plate has to land in it. A
    // hand-placement left standing would leave the machine sitting somewhere
    // else entirely while the announcement said which position it went to.
    if (placed) onMove(nodeId, null)
    announce(spoken)
  }

  useEffect(() => {
    const svg = svgRef.current
    if (!svg) return

    const toVB = (e: { clientX: number; clientY: number }) => {
      const r = svg.getBoundingClientRect()
      const vb = svg.viewBox.baseVal
      return {
        x: ((e.clientX - r.left) / r.width) * (vb.width || layout.width),
        y: ((e.clientY - r.top) / r.height) * (vb.height || layout.height),
        sx: (vb.width || layout.width) / r.width,
        sy: (vb.height || layout.height) / r.height,
      }
    }

    /** Scene space: the pan/zoom transform lives on `<g id="scene">`, so
     *  anything hit-testing against layout coordinates has to undo it. */
    const toScene = (e: { clientX: number; clientY: number }): Point => {
      const p = toVB(e)
      const { k, tx, ty } = view.current
      return { x: (p.x - tx) / k, y: (p.y - ty) / k }
    }

    const clamp = (v: number, a: number, b: number) => Math.max(a, Math.min(b, v))
    const zoomAt = (px: number, py: number, f: number) => {
      const base = fitRef.current.k
      const nk = clamp(view.current.k * f, base * ZOOM_MIN, base * ZOOM_MAX)
      if (nk === view.current.k) return
      userMoved.current = true
      const r = nk / view.current.k
      view.current.tx = px - (px - view.current.tx) * r
      view.current.ty = py - (py - view.current.ty) * r
      view.current.k = nk
      applyView()
    }

    const onWheel = (e: WheelEvent) => {
      e.preventDefault()
      const p = toVB(e)
      zoomAt(p.x, p.y, Math.exp(-e.deltaY * 0.0015))
    }

    const onPointerDown = (e: PointerEvent) => {
      const plate = (e.target as Element).closest?.('.machine,.band')
      if (plate) {
        const nodeId = plate.getAttribute('data-node')
        if (nodeId) {
          const card = layout.cards.find((c) => c.nodeId === nodeId)
          const band = layout.bands.find((b) => b.id === nodeId)
          const base = card
            ? card.offset
            : band
              ? band.offset
              : nodeId === PROVIDER_NODE_ID && layout.provider
                ? layout.provider.offset
                : { x: 0, y: 0 }
          cardDrag.current = {
            nodeId,
            from: toScene(e),
            base,
            dx: 0,
            dy: 0,
            moved: false,
          }
          svg.setPointerCapture(e.pointerId)
          return
        }
      }
      if ((e.target as Element).closest?.('.edge')) return
      drag.current = { x: e.clientX, y: e.clientY, tx: view.current.tx, ty: view.current.ty }
      svg.setPointerCapture(e.pointerId)
      svg.style.cursor = 'grabbing'
    }

    const onPointerMove = (e: PointerEvent) => {
      const cd = cardDrag.current
      if (cd) {
        const p = toScene(e)
        // The bus spans the full floor width by construction
        // (ClusterProvider.offset), so a horizontal drag has nowhere honest
        // to go -- the pointer can wander sideways without that ever
        // becoming part of the gesture, exactly as if it were pinned there.
        const dx = cd.nodeId === PROVIDER_NODE_ID ? 0 : p.x - cd.from.x
        const dy = p.y - cd.from.y
        if (!cd.moved && Math.hypot(dx, dy) < DRAG_SLOP) return
        if (!cd.moved) {
          cd.moved = true
          setDragging(cd.nodeId)
        }
        cd.dx = dx
        cd.dy = dy
        // The ghost is the only thing that moves while the pointer is down.
        // Re-laying the floor out per frame would re-aim every link and
        // re-span every band sixty times a second to show a plate the pointer
        // is still carrying; the drop is when the drawing has something true
        // to say about where the machine ended up.
        ghostRef.current?.setAttribute('transform', `translate(${dx.toFixed(1)} ${dy.toFixed(1)})`)
        return
      }
      if (!drag.current) return
      const p = toVB(e)
      userMoved.current = true
      view.current.tx = drag.current.tx + (e.clientX - drag.current.x) * p.sx
      view.current.ty = drag.current.ty + (e.clientY - drag.current.y) * p.sy
      applyView()
    }

    const onPointerEnd = () => {
      const cd = cardDrag.current
      cardDrag.current = null
      ghostRef.current?.removeAttribute('transform')
      if (cd) {
        setDragging(null)
        if (cd.moved) {
          lastPress.current = null
          const next = { x: cd.base.x + cd.dx, y: cd.base.y + cd.dy }
          // Dropped back where it started is a plate that is not hand-placed
          // any more, not an offset of zero: the stored record stays empty and
          // `Reset layout` goes back to offering nothing.
          const home = Math.abs(next.x) < 0.5 && Math.abs(next.y) < 0.5
          justPlaced.current = cd.nodeId
          onMove(cd.nodeId, home ? null : next)
          announce(`${name(cd.nodeId)} placed ${placement(home ? { x: 0, y: 0 } : next)}.`)
        } else if (cd.nodeId !== PROVIDER_NODE_ID) {
          // The bus stayed non-interactive to a plain click, and stays that
          // way here too -- there is no node sheet for it to open, and this
          // only fires at all because it now shares `.machine` with the
          // things that do have one.
          const band = layout.bands.find((b) => b.id === cd.nodeId)
          const at = performance.now()
          const prev = lastPress.current
          const doubled = prev && prev.nodeId === cd.nodeId && at - prev.at <= DOUBLE_MS
          if (band) {
            // A band selects and opens by served name, not by its own id --
            // the id is a deployment id or `remote:<name>`, and that is not
            // what selectDep/openSheet key on. Only a deployment has a sheet
            // to open (see Band's old onDoubleClick, now folded in here): a
            // provider-only band would ask the deployment inspector for a
            // deployment that does not exist.
            if (doubled && band.deploymentId != null) {
              lastPress.current = null
              openSheet({ kind: 'dep', id: band.servedName })
            } else {
              lastPress.current = { nodeId: cd.nodeId, at }
              selectDep(band.servedName)
            }
          } else if (doubled) {
            // Second press on the same plate opens the node sheet. The
            // selection the first press made is left standing rather than
            // toggled back off, so the machine the inspector is about stays
            // lit on the floor behind it.
            lastPress.current = null
            openSheet({ kind: 'node', id: cd.nodeId })
          } else {
            lastPress.current = { nodeId: cd.nodeId, at }
            selectNode(cd.nodeId)
          }
        }
      }
      drag.current = null
      svg.style.cursor = 'grab'
    }

    const onKeyDown = (e: KeyboardEvent) => {
      const step = e.shiftKey ? 45 : 15
      const vb = svg.viewBox.baseVal
      const c = { x: (vb.width || layout.width) / 2, y: (vb.height || layout.height) / 2 }
      const arrows: Record<string, [number, number]> = {
        ArrowLeft: [step, 0],
        ArrowRight: [-step, 0],
        ArrowUp: [0, step],
        ArrowDown: [0, -step],
      }
      // Alt+arrow on a focused machine moves it, and must not also pan.
      // The provider bus is also `.machine` now (see PROVIDER_NODE_ID) but
      // was never dealt a slot in `layout.arrangement`, so this is guarded
      // on actually having one -- `moveToSlot` on an id that was never in
      // the array would insert it, corrupting the persisted order for every
      // real node in it.
      if (e.altKey && arrows[e.key]) {
        const focused = (e.target as Element)?.closest?.('.machine')?.getAttribute('data-node')
        if (focused && layout.arrangement.includes(focused)) {
          e.preventDefault()
          const at = layout.arrangement.indexOf(focused)
          const cols = layout.cards.filter((cd) => cd.row === 0).length || 1
          const delta =
            e.key === 'ArrowLeft' ? -1 : e.key === 'ArrowRight' ? 1 : e.key === 'ArrowUp' ? -cols : cols
          const to = Math.max(0, Math.min(layout.arrangement.length - 1, at + delta))
          commitOrder(
            focused,
            to,
            `${name(focused)} moved to position ${to + 1} of ${layout.arrangement.length}.`,
          )
          return
        }
      }
      if (arrows[e.key]) {
        e.preventDefault()
        userMoved.current = true
        view.current.tx += arrows[e.key]![0]
        view.current.ty += arrows[e.key]![1]
        applyView()
      } else if (e.key === '+' || e.key === '=') {
        e.preventDefault()
        zoomAt(c.x, c.y, 1.2)
      } else if (e.key === '-' || e.key === '_') {
        e.preventDefault()
        zoomAt(c.x, c.y, 1 / 1.2)
      } else if (e.key === '0') {
        e.preventDefault()
        resetView()
      } else if (e.key === '[' || e.key === ']') {
        e.preventDefault()
        const edges = topology.edges
        if (edges.length === 0) return
        const keyOf = (a: string, b: string) => [a, b].sort().join('~')
        const cur = selLink == null ? -1 : edges.findIndex((edge) => keyOf(edge.src, edge.dst) === selLink)
        const nn = edges.length
        const next = edges[(((e.key === ']' ? cur + 1 : cur - 1) % nn) + nn) % nn]!
        selectLink(next.src, next.dst)
      }
    }

    svg.style.cursor = 'grab'
    svg.style.touchAction = 'none'
    svg.addEventListener('wheel', onWheel, { passive: false })
    svg.addEventListener('pointerdown', onPointerDown)
    svg.addEventListener('pointermove', onPointerMove)
    svg.addEventListener('pointerup', onPointerEnd)
    svg.addEventListener('pointercancel', onPointerEnd)
    svg.addEventListener('keydown', onKeyDown)
    return () => {
      svg.removeEventListener('wheel', onWheel)
      svg.removeEventListener('pointerdown', onPointerDown)
      svg.removeEventListener('pointermove', onPointerMove)
      svg.removeEventListener('pointerup', onPointerEnd)
      svg.removeEventListener('pointercancel', onPointerEnd)
      svg.removeEventListener('keydown', onKeyDown)
    }
  }, [layout, topology.edges, selLink, selectLink, selectNode, openSheet, onReorder, onMove, announce])

  if (box.w <= 0) {
    return (
      <div ref={hostRef} style={{ flex: 1, minHeight: 0 }}>
        <p className="unit" style={{ margin: 0, padding: '24px 0' }}>
          Drawing the cluster…
        </p>
      </div>
    )
  }

  if (layout.emptyMessage) {
    return (
      <div ref={hostRef} style={{ flex: 1, minHeight: 0 }}>
        <p className="unit" style={{ margin: 0, padding: '24px 0' }}>
          {layout.emptyMessage}
        </p>
      </div>
    )
  }

  const dragFrom = dragging
    ? layout.cards.find((c) => c.nodeId === dragging) ??
      layout.bands.find((b) => b.id === dragging) ??
      (dragging === PROVIDER_NODE_ID ? layout.provider : null)
    : null

  return (
    <div ref={hostRef} style={{ flex: 1, minHeight: 0 }}>
      <svg
        ref={svgRef}
        viewBox={`0 0 ${layout.width} ${layout.height}`}
        style={{ width: '100%', height: '100%', display: 'block' }}
        tabIndex={0}
        aria-label={`Cluster graph: ${layout.cards.length} machines, ${layout.edges.length} links drawn. Drag to pan, scroll to zoom. Drag a machine anywhere on the floor and it stays where you put it, or hold alt and press an arrow key to step it through the default order.`}
      >
        <g ref={sceneRef} id="scene">
          {/* The whole drawing, slid right so its ink sits in the middle of
              the viewBox (see layout.offsetX). It is inside the pan/zoom
              scene, so panning and zooming are unaffected, and it is a
              constant, so the drag maths -- which works in deltas against
              layout coordinates -- does not have to undo it. */}
          <g transform={`translate(${layout.offsetX} 0)`}>
            {/* Where the plate being carried was picked up from. There is no
                grid of drop targets any more because there is nothing to aim
                at -- it lands where it is let go -- so the one thing worth
                drawing is the outline it left behind, which is the only way
                to tell a small deliberate nudge from an accidental one. */}
            {dragFrom ? (
              <rect
                x={dragFrom.x - 3}
                y={dragFrom.y - 3}
                width={dragFrom.w + 6}
                height={dragFrom.h + 6}
                rx={6}
                fill="none"
                stroke="var(--rule)"
                strokeWidth={1}
                strokeDasharray="3 3"
              />
            ) : null}

            {/* Every band's leads, in ONE layer under everything else, rather
                than inside each band's own group. A lead runs from a machine
                plate down to its band, so it crosses every band stacked
                between the two -- and a group per band paints the fifth band's
                lead over the first four's plates, which is a grey hairline
                straight through a served name and its throughput figure. The
                white flow runs stay where they are, on top. */}
            {layout.bands.map((b) =>
              b.leads.map((pts, i) => (
                <path
                  key={`${b.id}-${i}`}
                  d={pts.map((p, j) => `${j === 0 ? 'M' : 'L'}${p.x} ${p.y}`).join(' ')}
                  stroke="var(--rule)"
                  strokeWidth={1}
                  fill="none"
                />
              )),
            )}

            {/* Flow furniture: flat square-cap runs, all of them. The butt-cap
                dash that used to mean "crosses the routing boundary" went with
                the boundary; the only dashes left in the drawing are on links
                nobody has measured, which is a different claim entirely. */}
            {layout.conns.map((c) => (
              <path
                key={c.id}
                d={c.dRender}
                fill="none"
                stroke="var(--ink)"
                strokeWidth={c.weight}
                strokeLinecap="square"
                opacity={c.opacity}
              />
            ))}

            {layout.edges.map((e) => (
              <Edge key={e.linkKey} edge={e} onSelect={selectLink} />
            ))}

            {/* Paint order is load-bearing: a packet slides under a plate and
                re-emerges, which is most of the animation's character. */}
            <g ref={particleLayerRef} id="particles" />

            {layout.junctions.map((j, i) => (
              <circle key={i} cx={j.x} cy={j.y} r={j.r} fill="var(--ink)" opacity={j.opacity} />
            ))}

            {/* One per endpoint family on this floor, not one per floor: a
                cluster serving speech and chat at once enters through two
                different routes and the drawing says so. */}
            {layout.entries.map((plate) => (
              <EntryPlate key={plate.modality} plate={plate} />
            ))}
            {layout.exits.map((plate) => (
              <ExitPlate key={plate.modality} plate={plate} />
            ))}

            {/* The band being dragged paints last, same reason PlateLayer
                orders a dragged card last: it rides over its neighbours
                instead of sliding underneath the ones drawn after it. */}
            {(dragging
              ? [...layout.bands].sort((a, b) => Number(a.id === dragging) - Number(b.id === dragging))
              : layout.bands
            ).map((b) =>
              dragging === b.id ? (
                <g key={b.id} ref={ghostRef} opacity={0.92}>
                  <Band band={b} dragging onSelect={selectDep} />
                </g>
              ) : (
                <Band key={b.id} band={b} dragging={false} onSelect={selectDep} />
              ),
            )}

            <BandLaunchLayer bands={layout.bands} deployments={live} />

            <PlateLayer
              layout={layout}
              topologyNodes={topology.nodes}
              nodes={nodes}
              deployments={live}
              routing={routing}
              dragging={dragging}
              ghostRef={ghostRef}
              onSelect={selectNode}
              onOpen={(id) => openSheet({ kind: 'node', id })}
            />

            {layout.provider ? (
              dragging === PROVIDER_NODE_ID ? (
                // Same trick PlateLayer plays for a dragged card: only the
                // ghost moves while the pointer is down (see onPointerMove),
                // so this group takes the transform instead of the bus
                // itself re-laying out on every frame.
                <g ref={ghostRef} opacity={0.92}>
                  <ProviderBus provider={layout.provider} providerById={providerById} dragging />
                </g>
              ) : (
                <ProviderBus provider={layout.provider} providerById={providerById} dragging={false} />
              )
            ) : null}
          </g>
        </g>
        <ParticleField
          layerRef={particleLayerRef}
          routing={routing}
          paths={layout.paths}
          bands={layout.bands}
        />
      </svg>
    </div>
  )
})

// ── Links ────────────────────────────────────────────────────────────────────

function Edge({ edge, onSelect }: { edge: ClusterEdge; onSelect: (a: string, b: string) => void }) {
  const b = edge.bracket
  return (
    <g
      className="edge"
      data-link={edge.linkKey}
      role="button"
      tabIndex={0}
      aria-label={edge.aria}
      onClick={() => onSelect(edge.src, edge.dst)}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          onSelect(edge.src, edge.dst)
        }
      }}
    >
      <title>{edge.aria}</title>
      {b ? (
        <>
          {/* An 18-tall invisible plate, so a 7-unit bar is still clickable. */}
          <rect x={b.x} y={b.hitY} width={b.w} height={b.hitH} fill="var(--panel)" opacity={0} />
          <rect
            x={b.x}
            y={b.y}
            width={b.w}
            height={b.h}
            fill="var(--ink)"
            opacity={0.16}
            stroke={edge.measured ? undefined : 'var(--ink)'}
            strokeWidth={edge.measured ? undefined : 1}
            strokeDasharray={edge.measured ? undefined : '3 3'}
          />
          {b.fraction > 0 ? (
            <rect x={b.x} y={b.y} width={b.w * b.fraction} height={b.h} fill="var(--ink)" />
          ) : null}
        </>
      ) : (
        <>
          {/* A fat transparent hit area, so a 1-unit line is still clickable.
              It keeps the squared-off `d`: what is clickable should be the
              route, not the fillet drawn over it. */}
          <path d={edge.d} fill="none" stroke="transparent" strokeWidth={12} />
          <path
            className="wire"
            d={edge.dRender}
            fill="none"
            stroke={edge.measured ? 'var(--ink)' : 'var(--ink-muted)'}
            strokeWidth={edge.selected ? edge.width + 1 : edge.width}
            strokeDasharray={edge.dashed ? '4 4' : undefined}
            opacity={edge.opacity}
            /* The corners are filleted in the geometry (`roundedPath`), so
               this only ever has the fillet's own joins to soften. Left in
               because a corner too short to round is still drawn mitred. */
            strokeLinejoin="round"
          />
        </>
      )}
      {edge.showLabel ? (
        <Caption cx={edge.labelAt.x} y={edge.labelAt.y} text={edge.label} outlined={edge.selected} />
      ) : null}
    </g>
  )
}

/** A caption with its own knockout plate. Every caption in this drawing is
 *  wider than the channel it sits in, so without one it is ink on a filled
 *  plate. Plate first, text second. */
function Caption({ cx, y, text, outlined }: { cx: number; y: number; text: string; outlined?: boolean }) {
  const w = plateWidth(text)
  return (
    <>
      <rect
        x={cx - w / 2}
        y={y - 9}
        width={w}
        height={13}
        rx={2}
        fill="var(--panel)"
        stroke={outlined ? 'var(--select-on-panel)' : 'none'}
      />
      <text x={cx} y={y} className="m" fontSize={9} fill="var(--ink)" textAnchor="middle">
        {text}
      </text>
    </>
  )
}

// ── Flow furniture ───────────────────────────────────────────────────────────

function EntryPlate({ plate }: { plate: EndpointPlate }) {
  return (
    <g>
      <title>{`Requests for ${plate.modality} models arrive here.`}</title>
      <rect x={plate.x} y={plate.y} width={plate.w} height={plate.h} rx={4} fill="var(--fill)" />
      {/* One line, not two: the endpoint's path is the label, and the second
          line is deliberately gone. Baseline is the box's own middle so
          dropping it does not leave the remaining line sitting high.
          It is now the WHOLE route -- the truncation that used to be here
          read as an endpoint that does not exist -- and it comes off the
          plate rather than out of a constant, because a floor can have more
          than one endpoint on it. */}
      <text
        x={plate.x + ENTRY_PAD}
        y={plate.y + plate.h / 2 + 4}
        className="m"
        fontSize={ENTRY_FONT}
        fill="var(--on-fill)"
      >
        {plate.label}
      </text>
    </g>
  )
}

/** The response leaving. Mirror of the entry plate, and the half of the picture
 *  that was missing: without it a request arrived, walked its machines and
 *  stopped, so the tokens streaming back out -- the thing the cluster spends
 *  its time doing -- were nowhere in the drawing. */
function ExitPlate({ plate }: { plate: EndpointPlate }) {
  return (
    <g>
      <title>
        {plate.modality === 'speech'
          ? 'The response leaving: one audio file per request, not a token stream.'
          : 'The response leaving. Blocks on the return legs are output tokens.'}
      </title>
      <rect x={plate.x} y={plate.y} width={plate.w} height={plate.h} rx={4} fill="var(--fill)" />
      <text
        x={plate.x + EXIT_PAD}
        y={plate.y + plate.h / 2 + 4}
        className="m"
        fontSize={EXIT_FONT}
        fill="var(--on-fill)"
      >
        {plate.label}
      </text>
    </g>
  )
}

/** A provider is ONE endpoint that can serve anything, so it is a bus, not a
 *  rack of them -- one box for every provider on it, not one per provider.
 *  It is drawn exactly like a machine plate now (filled, same hairline
 *  border, same hover/focus ring) and can be dragged the same way, vertically
 *  along the rail it already sits on; see PROVIDER_NODE_ID and
 *  ClusterProvider.offset. What still tells it apart from a real node is
 *  everything a machine plate could never show truthfully for one: no
 *  memory meter, no signal-coloured border (it carries no health reading to
 *  colour one with), no served-name occupant line. */
function ProviderBus({
  provider,
  providerById,
  dragging,
}: {
  provider: NonNullable<ClusterLayout['provider']>
  providerById: Map<string, Provider>
  dragging: boolean
}) {
  // One subscription for the box, not one per row: a late 404 repaints the bus
  // once instead of each row holding its own state.
  const [, bump] = useState(0)
  useEffect(() => subscribeProviderLogos(() => bump((n) => n + 1)), [])
  return (
    <g
      className="machine"
      data-node={PROVIDER_NODE_ID}
      data-dragging={dragging ? 'true' : undefined}
      tabIndex={0}
      aria-label="Providers"
    >
      <title>Providers</title>
      <rect
        className="ring"
        x={provider.x - 3}
        y={provider.y - 3}
        width={provider.w + 6}
        height={provider.h + 6}
        rx={6}
        fill="none"
      />
      {/* Fill and border together: a .55 group on the border alone put this
          box's primary line at 3.70:1, so opacity stays on the pair rather
          than being spread across text that carries its own colour. */}
      <g opacity={provider.active ? 1 : 0.55}>
        <rect x={provider.x} y={provider.y} width={provider.w} height={provider.h} rx={4} fill="var(--fill)" />
        {/* No signal to colour this with, unlike a machine's own state
            border -- the hairline a healthy one gets is the only border this
            box could ever honestly draw. */}
        <rect
          x={provider.x}
          y={provider.y}
          width={provider.w}
          height={provider.h}
          rx={4}
          fill="none"
          stroke="var(--rule)"
          strokeWidth={1.5}
        />
      </g>
      <text
        x={provider.x + PROVIDER_TEXT_X}
        y={provider.y + 15}
        className="m"
        fontSize={9}
        fill={provider.active ? 'var(--on-fill)' : 'var(--on-fill-dim)'}
      >
        {provider.label}
      </text>
      <text
        x={provider.x + PROVIDER_TEXT_X}
        y={provider.y + 30}
        className="m"
        fontSize={9}
        fill="var(--on-fill-dim)"
      >
        {provider.sublabel}
      </text>
      {/* One line per provider, naming what it serves here, and each one
          carrying that provider's own mark. This is the whole of what the box
          could not say before: with two providers on the rail it named the
          count and nothing else.

          The mark is one PER ROW, never one for the box. The rows are the
          providers; a box-level mark would have to pick one arbitrarily the
          moment there are two -- and the box exists precisely because "a
          provider" is plural. With exactly one provider the two designs render
          identically, so per-row costs nothing in the common case and is the
          only one that works in the case this box was rebuilt for.

          A provider's logo is coloured artwork, and on this drawing colour
          means something is WRONG. Three things keep that true:
            1. It never leaves an 11-unit tile inside the bus's own box. It
               touches no plate, no band and no link.
            2. That box carries no state marks at all -- health has nothing
               to read here, and its only variable is opacity -- so a
               coloured mark inside it cannot be mistaken for one.
            3. Nothing is DERIVED from it: no dominant-colour read, no tinted
               row, no tinted border. It is a picture in a box, not a colour
               source. That is the line. */}
      {provider.rows.map((row, i) => {
        const y = provider.y + 43 + i * PROVIDER_ROW_H
        const meta = providerById.get(row.providerId)
        return (
          <g key={row.providerId} opacity={row.active ? 1 : 0.55}>
            <title>{meta ? `${meta.display_name} · ${meta.kind}` : row.providerId}</title>
            {/* Drawn ALWAYS, with the mark painted over it. A failed <image>
                in SVG renders as nothing at all, so this is what stops a 404,
                a slow load and an older coordinator all rendering a gap. */}
            <rect
              x={provider.x + PROVIDER_TEXT_X}
              y={y + PROVIDER_LOGO_DY}
              width={PROVIDER_LOGO}
              height={PROVIDER_LOGO}
              rx={2}
              fill="none"
              stroke="var(--on-fill)"
              strokeWidth={1}
            />
            <text
              x={provider.x + PROVIDER_TEXT_X + PROVIDER_LOGO / 2}
              y={y}
              textAnchor="middle"
              className="m"
              fontSize={9}
              fill="var(--on-fill)"
            >
              {providerMonogram(row.providerId, meta?.display_name)}
            </text>
            {providerLogoMissing(row.providerId) ? null : (
              <image
                href={providerLogoUrl(row.providerId)}
                x={provider.x + PROVIDER_TEXT_X}
                y={y + PROVIDER_LOGO_DY}
                width={PROVIDER_LOGO}
                height={PROVIDER_LOGO}
                preserveAspectRatio="xMidYMid meet"
                onError={() => markProviderLogoMissing(row.providerId)}
              />
            )}
            <text
              x={provider.x + PROVIDER_ROW_TEXT_X}
              y={y}
              className="m"
              fontSize={9}
              fill={row.active ? 'var(--on-fill)' : 'var(--on-fill-dim)'}
            >
              {row.text}
            </text>
          </g>
        )
      })}
    </g>
  )
}

// ── Deployment bands ─────────────────────────────────────────────────────────

/** The band is the mockup's served-name plate, stretched across the machines
 *  the deployment occupies. Every deployment gets one, single-node included --
 *  the entry box connects to bands, never to machines, so a solo deployment
 *  without one would have no place in the request flow. */
function Band({
  band,
  dragging,
  onSelect,
}: {
  band: ClusterBand
  dragging: boolean
  onSelect: (name: string) => void
}) {
  const stroke = band.degraded ? 'var(--warn-solid)' : 'var(--rule)'
  // One id per band. A served name can carry '/' and '.', which are legal in
  // an id but not in a url(#...) reference, so it is spelled out rather than
  // interpolated raw.
  const clipId = `bandclip-${band.id.replace(/[^A-Za-z0-9_-]/g, '_')}`
  // Where it runs, in the same sentence for both kinds: machines when we have
  // them, the providers when the hardware is somebody else's. A band that said
  // "on " and stopped was the first thing an off-cluster model got wrong.
  const where = band.members.length
    ? `on ${band.members.join(' and ')}`
    : band.providers.length
      ? `on ${band.providers.join(' and ')}`
      : 'nowhere yet'
  const backedUp = band.kind === 'local' && band.providers.length > 0
  // The stepper is drawn by a layer above this group and is not readable from
  // here, so the one fact it carries -- that this name is not answering yet --
  // is said in the band's own label instead. Without it a launching band and a
  // serving one sound identical to a screen reader.
  const label = `${band.servedName}${band.plan ? `, ${band.plan}` : ''}, ${where}${
    backedUp ? `, backed up by ${band.providers.join(' and ')}` : ''
  }${band.loading ? ', still starting' : ''}`
  return (
    <g
      className="band"
      data-band={band.id}
      data-node={band.id}
      data-dragging={dragging ? 'true' : undefined}
      role="button"
      tabIndex={0}
      aria-label={label}
      /* No onClick/onDoubleClick here -- pointer capture on the SVG resolves
         both, exactly as it does for a machine plate (see onPointerDown):
         a press that does not move far enough to count as a drag selects on
         one, opens the deployment sheet on two within DOUBLE_MS. Only the
         keyboard path, which capture never touches, stays on the element. */
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          onSelect(band.servedName)
        }
      }}
    >
      <title>{label}</title>
      <rect className="ring" x={band.x - 3} y={band.y - 3} width={band.w + 6} height={band.h + 6} rx={5} fill="none" />
      <rect
        x={band.x}
        y={band.y}
        width={band.w}
        height={band.h}
        rx={3}
        fill="var(--fill)"
        stroke={stroke}
        strokeWidth={1}
        /* No dash. An off-cluster band used to be drawn dashed because dash
           meant "crosses the routing boundary"; the boundary is gone and the
           dash with it, and the band's own words -- "via openrouter", "off
           cluster" -- say whose hardware it is more plainly than a stroke
           pattern did. The only dashes left in this drawing are on links
           nobody has measured, which is a different claim entirely. */
        /* An open band spans machines it does not occupy. Fading the bar and
           ticking each member keeps "spans" from reading as "occupies". */
        opacity={band.contiguous ? 1 : 0.6}
      />
      {band.contiguous
        ? null
        : band.ticks.map((x, i) => (
            <rect key={i} x={x - 10} y={band.y} width={20} height={2.5} fill="var(--on-fill)" />
          ))}
      {band.selected ? (
        <rect
          x={band.x - 3}
          y={band.y - 3}
          width={band.w + 6}
          height={band.h + 6}
          rx={5}
          fill="none"
          stroke="var(--select-on-panel)"
          strokeWidth={1.5}
        />
      ) : null}
      {/* The band is now sized to hold both of these lines AND its readout
          (layout.ts `bandNeed`), so this clip is a backstop rather than a
          policy: it is what the drawing does if the browser's font is not the
          0.6em mono every width here is measured from.

          A band used to be exactly as wide as the machines it occupies, which
          has nothing to do with how long its name is: `Qwen2.5-0.5B-Instruct`
          on a one-machine band ran the name underneath its own throughput
          figure and made both unreadable. The bar grows for its words now,
          and the leads under it go on saying which machines are its own.
          `bandLabelWidth` still keeps the clip clear of the readout, so if it
          ever does bite it takes the tail of a name and not the leading digit
          of a number. */}
      <clipPath id={clipId}>
        <rect x={band.x} y={band.y} width={bandLabelWidth(band.w)} height={band.h} />
      </clipPath>
      <g clipPath={`url(#${clipId})`}>
        <text x={band.x + 11} y={band.y + 15} className="m" fontSize={10} fill="var(--on-fill)">
          {band.servedName}
        </text>
        <text x={band.x + 11} y={band.y + 29} className="m" fontSize={9} fill="var(--on-fill-dim)">
          {bandSubline(band)}
        </text>
      </g>
      {/* A band that is still arriving has no throughput to report, so the
          readout's corner carries how long it has been arriving instead. The
          two never share the corner: a tok/s figure beside a stepper would be
          a rate for a model that is not answering yet. */}
      {band.loading ? <BandSweep band={band} /> : <BandThroughput band={band} />}
    </g>
  )
}

/** Its own component so a 1 Hz frame re-renders the number and not the band it
 *  sits on. */
/** A band's measured output rate off the 1 Hz frame.
 *
 *  Extracted so the readout and the return-leg animation cannot drift: the
 *  number on the band and the blocks leaving it are the same measurement, and
 *  the drawing says they are, so they had better come from one expression.
 *
 *  Both kinds read the same frame, off the same StatsRegistry the gateway
 *  counts every request through. A remote band is summed from its targets
 *  rather than joined to /api/topology's copy of the figure: that one is
 *  honest but arrives on a 5s poll, and a band ticking five times slower than
 *  the one above it reads as a fault in the drawing. */
export function bandTokensPerSec(
  band: ClusterBand,
  frame: ReturnType<typeof useMetrics>['frame'],
): number | undefined {
  if (band.kind === 'local') {
    return frame?.deployments.find((d) => d.deployment_id === band.deploymentId)?.tokens_per_sec ?? undefined
  }
  if (frame == null) return undefined
  return band.targetIds.reduce(
    (sum, id) => sum + (frame.remotes?.find((r) => r.target_id === id)?.tokens_per_sec ?? 0),
    0,
  )
}

function BandThroughput({ band }: { band: ClusterBand }) {
  const { frame, stale } = useMetrics()
  const tps = bandTokensPerSec(band, frame)
  return (
    <>
      <text
        x={band.x + band.w - 11}
        y={band.y + 17}
        textAnchor="end"
        className="m"
        fontSize={13}
        fill={stale ? 'var(--on-fill-dim)' : 'var(--on-fill)'}
      >
        {fmt(tps, 1)}
      </text>
      <text x={band.x + band.w - 11} y={band.y + 29} textAnchor="end" className="m" fontSize={9} fill="var(--on-fill-dim)">
        tok/s
      </text>
    </>
  )
}

// ── Machines ─────────────────────────────────────────────────────────────────

/** The floor moving from one layout to the next.
 *
 *  Imperative and outside React state, the way `particles.ts` and `applyView`
 *  already are: a tween that went through `useState` would re-render the whole
 *  drawing sixty times a second to move two plates, and re-laying the floor
 *  out per frame is exactly what the drag path refuses to do.
 *
 *  Plates and bands move by the FLIP trick: they are rendered where they have
 *  ARRIVED, pushed back to where they were with an inline transform, and the
 *  push is then dropped so the stylesheet's transition carries it to nothing.
 *  Nothing inside a plate has to know -- which matters, because a plate is a
 *  dozen absolutely-placed primitives and there is no useful way to transition
 *  a dozen sibling `x` attributes.
 *
 *  Wires cannot be done that way: a path's two ends move by different amounts,
 *  so there is no single transform that takes it there. They are interpolated
 *  vertex for vertex instead, and a pair whose route CHANGED SHAPE is snapped
 *  rather than morphed (`sameShape`). During the tween a wire is drawn without
 *  its crossing gaps: where two wires cross is a fact about the finished
 *  drawing, and a hole that slid along a wire while it moved would be read as
 *  something happening on the link.
 *
 *  None of this decides where anything ends up. `layoutCluster` still does,
 *  and it is still the same pure function of the node set and the arrangement
 *  it was -- a machine is in the same place every time somebody looks, and
 *  this is only about how it got there. */
function useFloorMotion(
  layout: ClusterLayout,
  sceneRef: RefObject<SVGGElement>,
  justPlaced: MutableRefObject<string | null>,
) {
  const prev = useRef<{
    cards: Map<string, Point>
    bands: Map<string, Point>
    routes: Map<string, Point[]>
    w: number
    h: number
  } | null>(null)
  const frame = useRef(0)
  /** Puts an interrupted tween on its final geometry. A wire is moved by
   *  writing `d` behind React's back, so a re-render that happens to carry the
   *  same `dRender` React last put up will not write it again -- and the wire
   *  would be left wherever the cancelled tween had got to. */
  const settle = useRef<(() => void) | null>(null)

  useLayoutEffect(() => {
    const scene = sceneRef.current
    const now = {
      cards: new Map(layout.cards.map((c): [string, Point] => [c.nodeId, { x: c.x, y: c.y }])),
      bands: new Map(layout.bands.map((b): [string, Point] => [b.id, { x: b.x, y: b.y }])),
      routes: new Map(
        layout.edges
          .filter((e) => e.kind === 'path')
          .map((e): [string, Point[]] => [e.linkKey, e.pts]),
      ),
      w: layout.width,
      h: layout.height,
    }
    const was = prev.current
    prev.current = now
    const dropped = justPlaced.current
    justPlaced.current = null

    cancelAnimationFrame(frame.current)
    settle.current?.()
    settle.current = null

    if (!scene || !was) return
    // A resize re-frames the whole drawing at once, and animating that is a
    // floor that wobbles under the pointer while somebody drags a panel edge.
    if (was.w !== now.w || was.h !== now.h) return
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return

    // Indexed off `dataset` rather than an attribute selector: a node id and a
    // served name are somebody else's strings, and one with a quote in it
    // would be a selector that throws rather than a plate that does not move.
    const index = (sel: string, key: string) => {
      const out = new Map<string, SVGGElement>()
      for (const el of Array.from(scene.querySelectorAll<SVGGElement>(sel))) {
        const v = el.dataset[key]
        if (v) out.set(v, el)
      }
      return out
    }

    const pushed: SVGGElement[] = []
    const push = (el: SVGGElement | undefined, by: Point) => {
      if (!el) return
      el.style.transition = 'none'
      el.style.transform = `translate(${by.x}px, ${by.y}px)`
      pushed.push(el)
    }

    const plates = index('.machine', 'node')
    for (const [nodeId, by] of shifts(was.cards, now.cards)) {
      // The plate the pointer just put down is already under the pointer.
      if (nodeId === dropped) continue
      push(plates.get(nodeId), by)
    }
    const bands = index('.band', 'band')
    for (const [id, by] of shifts(was.bands, now.bands)) {
      // Same reason as the plate above: a band that was just hand-placed is
      // already sitting exactly where the ghost left it, so pushing it back
      // to its old spot and tweening it forward again would replay a move
      // that already happened under the pointer.
      if (id === dropped) continue
      push(bands.get(id), by)
    }

    const finals = new Map(layout.edges.map((e) => [e.linkKey, e.dRender]))
    const wires: { el: SVGPathElement; from: Point[]; to: Point[]; final: string }[] = []
    for (const [key, el] of index('.edge', 'link')) {
      const from = was.routes.get(key)
      const to = now.routes.get(key)
      const path = el.querySelector<SVGPathElement>('path.wire')
      if (!from || !to || !path || !sameShape(from, to)) continue
      if (from.every((q, i) => q.x === to[i]!.x && q.y === to[i]!.y)) continue
      wires.push({ el: path, from, to, final: finals.get(key) ?? '' })
    }

    if (!pushed.length && !wires.length) return

    // Lay the push down before taking it away. Without the read the browser
    // coalesces the two writes and there is nothing left to animate.
    void scene.getBoundingClientRect()
    for (const el of pushed) {
      el.style.transition = ''
      el.style.transform = ''
    }

    const dur = floorDuration()
    const started = performance.now()
    settle.current = () => {
      for (const w of wires) w.el.setAttribute('d', w.final)
    }
    const step = () => {
      const t = Math.min(1, (performance.now() - started) / dur)
      const at = ease(t)
      for (const w of wires) {
        const route = lerpRoute(w.from, w.to, at)
        w.el.setAttribute('d', route ? roundedPath(route, CORNER_R) : w.final)
      }
      if (t < 1) frame.current = requestAnimationFrame(step)
      else settle.current?.()
    }
    step()
    return () => cancelAnimationFrame(frame.current)
  }, [layout, sceneRef, justPlaced])
}

/** How long the floor takes to move, read off the token the stylesheet uses
 *  for the plates rather than spelled here a second time -- a wire tweened to
 *  its own number would arrive without them. */
function floorDuration(): number {
  const raw = getComputedStyle(document.documentElement).getPropertyValue('--dur').trim()
  const ms = raw.endsWith('ms') ? parseFloat(raw) : raw.endsWith('s') ? parseFloat(raw) * 1000 : NaN
  return Number.isFinite(ms) && ms > 0 ? ms : 180
}

function PlateLayer({
  layout,
  topologyNodes,
  nodes,
  deployments,
  routing,
  dragging,
  ghostRef,
  onSelect,
  onOpen,
}: {
  layout: ClusterLayout
  topologyNodes: TopologyNode[]
  nodes: NodeStateDTO[]
  deployments: DeploymentDTO[]
  routing: RoutingConfig[]
  dragging: string | null
  ghostRef: RefObject<SVGGElement>
  onSelect: (id: string) => void
  onOpen: (id: string) => void
}) {
  const { frame, stale } = useMetrics()

  // The plate being dragged paints last so it rides over its neighbours
  // instead of sliding underneath the ones that come after it.
  const ordered = dragging
    ? [...layout.cards].sort((a, b) => Number(a.nodeId === dragging) - Number(b.nodeId === dragging))
    : layout.cards

  return (
    <>
      {ordered.map((card) => {
        const body = (
          <MachinePlate
            key={card.nodeId}
            card={card}
            topo={topologyNodes.find((n) => n.node_id === card.nodeId)}
            state={nodes.find((n) => n.profile.node_id === card.nodeId)}
            frame={frame}
            stale={stale}
            dragging={dragging === card.nodeId}
            subline={layout.subline}
            share={share(routing, card.nodeId)}
            servedNames={servedOn(deployments, card.nodeId)}
            onSelect={onSelect}
            onOpen={onOpen}
          />
        )
        return dragging === card.nodeId ? (
          <g key={card.nodeId} ref={ghostRef} opacity={0.92}>
            {body}
          </g>
        ) : (
          body
        )
      })}
    </>
  )
}

/** Read off the deployments rather than the bands: a machine names what it is
 *  running whether or not that deployment spans anything. */
function servedOn(deployments: DeploymentDTO[], nodeId: string): string[] {
  return [...new Set(deployments.filter((d) => d.node_ids.includes(nodeId)).map((d) => d.served_name))].sort()
}

/** The share of traffic this machine takes, when its served name routes by a
 *  policy under which `weight` is actually a configured share. Zero is a real
 *  answer and is drawn, not hidden. */
function share(configs: RoutingConfig[], nodeId: string): { served_name: string; weight: number } | null {
  for (const cfg of configs) {
    if (!PROPORTIONAL.has(cfg.policy)) continue
    const t = cfg.targets.find((x) => x.kind === 'local' && x.node_ids?.includes(nodeId))
    if (t) return { served_name: cfg.served_name, weight: t.weight }
  }
  return null
}

function MachinePlate({
  card,
  topo,
  state,
  frame,
  stale,
  dragging,
  subline,
  share: shareOf,
  servedNames,
  onSelect,
  onOpen,
}: {
  card: PlacedCard
  topo: TopologyNode | undefined
  state: NodeStateDTO | undefined
  frame: ReturnType<typeof useMetrics>['frame']
  stale: boolean
  dragging: boolean
  /** Authored units reserved on every full-tier plate for the identity line.
   *  Reserved floor-wide (see layout.SUBLINE_H), so a plate with nothing to
   *  put there still shifts its rows down and stays aligned with its row. */
  subline: number
  share: { served_name: string; weight: number } | null
  servedNames: string[]
  onSelect: (id: string) => void
  onOpen: (id: string) => void
}) {
  const live = state ? nodeLive(state, frame, stale) : null
  const signal = state && live ? nodeSignal(state, live) : topo?.state === 'unreachable' ? 'fault' : 'live'

  // Colour means something is WRONG. A healthy machine gets the same hairline
  // every other healthy container in the app gets, so the one that is not
  // healthy is the thing your eye lands on.
  const stroke = signal === 'live' ? 'var(--rule)' : `var(--${signal}-solid)`

  // memory_used_pct arrives as null on the wire for a node that has never
  // reported, and nodeLive can produce a non-finite value when a node's
  // addressable memory is zero. Both are "no reading", never a live-looking 0.
  const rawMem = live?.memory_used_pct ?? topo?.memory_used_pct ?? null
  const mem = typeof rawMem === 'number' && Number.isFinite(rawMem) ? Math.max(0, Math.min(100, rawMem)) : null
  // What to call it, and what the name is hiding. `hostname` is deliberately
  // NOT the name: a worker in a --network host container reports the host's
  // hostname, so naming plates that way draws two machines with one name (see
  // state/names.ts). The id everything else keys by goes underneath.
  const name = nodeName(topo ?? { node_id: card.nodeId }, card.nodeId)
  const identity = nodeSubtitle(topo ?? { node_id: card.nodeId }, topo?.hostname)
  // Only the tier the floor reserved room on can draw it; on the others the
  // tooltip and the node sheet carry it.
  const showsIdentity = card.bodyTier === 'full' && subline > 0 && identity !== ''
  const dy = card.bodyTier === 'full' ? subline : 0
  // Both from layout.ts, which measured the plate's width from these exact
  // strings. A second spelling here is how a box stops fitting what it draws.
  const occupant = plateOccupant(servedNames)

  const tier = card.bodyTier
  const trackW = card.w - 22
  // The tooltip and the screen reader always get the full identity, at every
  // tier, whether or not the plate had room to draw it.
  const label =
    `${name}${identity ? ` (${identity})` : ''}` +
    `${topo?.hostname && topo.hostname !== identity && topo.hostname !== name ? `, host ${topo.hostname}` : ''}` +
    `, ${topo?.role ?? 'machine'}, ${topo?.state ?? 'unknown'}` +
    `, memory ${pct(mem)} percent, position ${card.slot + 1}`

  // No onClick/onDoubleClick here: pointer capture on the SVG retargets both
  // to it (see DOUBLE_MS), so a press on a plate is resolved in the pointer
  // handler instead -- one press selects, two open the node sheet. Only the
  // keyboard path, which capture never touches, stays on the element, and it
  // mirrors that split: Space is the single press (light the plate), Enter is
  // the double one (open the sheet). Since the rail below the graph no longer
  // shows a machine, Enter is the whole keyboard route to a machine's
  // readouts, so it must open the sheet rather than merely select.
  return (
    <g
      className="machine"
      data-node={card.nodeId}
      data-dragging={dragging ? 'true' : undefined}
      role="button"
      tabIndex={0}
      aria-label={label}
      onKeyDown={(e) => {
        if (e.key === 'Enter') {
          e.preventDefault()
          onOpen(card.nodeId)
        } else if (e.key === ' ') {
          e.preventDefault()
          onSelect(card.nodeId)
        }
      }}
    >
      <title>{label}</title>
      <rect className="ring" x={card.x - 3} y={card.y - 3} width={card.w + 6} height={card.h + 6} rx={6} fill="none" />
      {card.selected ? (
        <rect
          x={card.x - 3}
          y={card.y - 3}
          width={card.w + 6}
          height={card.h + 6}
          rx={6}
          fill="none"
          stroke="var(--select-on-panel)"
          strokeWidth={1.5}
        />
      ) : null}

      <rect x={card.x} y={card.y} width={card.w} height={card.h} rx={4} fill="var(--fill)" />
      {/* The state border. Nothing else on this plate is a state. */}
      <rect
        x={card.x}
        y={card.y}
        width={card.w}
        height={card.h}
        rx={4}
        fill="none"
        stroke={stroke}
        strokeWidth={1.5}
      />

      <text x={card.x + 11} y={card.y + 15} className="m" fontSize={9} fill="var(--on-fill)">
        {name}
      </text>
      {showsIdentity ? (
        <text x={card.x + 11} y={card.y + 26} className="m" fontSize={8} fill="var(--on-fill-dim)">
          {identity}
        </text>
      ) : null}
      <text
        x={card.x + card.w - 11}
        y={card.y + 15}
        textAnchor="end"
        className="m"
        fontSize={9}
        fill={servedNames.length ? 'var(--on-fill)' : 'var(--on-fill-dim)'}
      >
        {occupant}
      </text>

      {/* The meter: one colour, two opacities, inset to the same 11 the text
          uses, so it reads as one recessed slot milled into the plate rather
          than two stacked rectangles. A reading we do not have is an empty
          dashed track, never a filled zero. */}
      {mem == null ? (
        <rect
          x={card.x + 11}
          y={card.y + 21 + dy}
          width={trackW}
          height={14}
          rx={2}
          fill="none"
          stroke="var(--on-fill)"
          strokeWidth={1}
          strokeDasharray="3 3"
          opacity={0.45}
        />
      ) : (
        <>
          <rect x={card.x + 11} y={card.y + 21 + dy} width={trackW} height={14} rx={2} fill="var(--on-fill)" opacity={0.18} />
          <rect
            x={card.x + 11}
            y={card.y + 21 + dy}
            width={(trackW * mem) / 100}
            height={14}
            rx={2}
            fill="var(--on-fill)"
            opacity={0.85}
          />
        </>
      )}

      {/* The two live rows. The plate reserved room for these from a template
          of their widest form (layout.ts POWER_ROW and UTIL_ROW) rather than
          from the figures themselves, which change every second -- keep the
          templates in step with what is written here. */}
      {tier !== 'chip' ? (
        <text x={card.x + 11} y={card.y + 47 + dy} className="m" fontSize={9} fill="var(--on-fill-dim)">
          {`${fmt(live?.power_w ?? topo?.power_w, 0)} W · ${fmt(live?.temp_c ?? topo?.temp_c, 0)} °C`}
        </text>
      ) : null}

      {tier === 'full' ? (
        <>
          <text x={card.x + 11} y={card.y + 61 + dy} className="m" fontSize={9} fill="var(--on-fill-dim)">
            {`${utilKind(state?.profile ?? topo)} ${fmt(live?.util_pct ?? topo?.util_pct, 0)}% · ${pct(mem)}% memory`}
          </text>
          {shareOf ? (
            <ShareBar card={card} trackW={trackW} dy={dy} share={shareOf} />
          ) : (
            <text x={card.x + 11} y={card.y + 74 + dy} className="m" fontSize={9} fill="var(--on-fill-dim)">
              {plateSpec(topo)}
            </text>
          )}
        </>
      ) : null}
    </g>
  )
}

/** Under weighted routing an unequal split should be visible rather than
 *  mysterious. Same meter grammar as the memory bar above it, half the height,
 *  so the plate carries one visual vocabulary rather than two. */
function ShareBar({
  card,
  trackW,
  dy,
  share: shareOf,
}: {
  card: PlacedCard
  trackW: number
  /** The plate's identity-line shift, so this rides with the rows above it. */
  dy: number
  share: { served_name: string; weight: number }
}) {
  const w = trackW - 30
  const clamped = Math.max(0, Math.min(1, shareOf.weight))
  return (
    <g>
      <title>{`${shareOf.served_name}: ${pct(shareOf.weight * 100)} percent of traffic`}</title>
      <rect x={card.x + 11} y={card.y + 68 + dy} width={w} height={6} rx={2} fill="var(--on-fill)" opacity={0.18} />
      <rect x={card.x + 11} y={card.y + 68 + dy} width={w * clamped} height={6} rx={2} fill="var(--on-fill)" opacity={0.85} />
      <text
        x={card.x + card.w - 11}
        y={card.y + 74 + dy}
        textAnchor="end"
        className="m"
        fontSize={9}
        fill="var(--on-fill-dim)"
      >
        {`${pct(shareOf.weight * 100)}%`}
      </text>
    </g>
  )
}

/** The particle emitter for the whole floor. Its whole job is to turn measured
 *  per-target request counts into the `TargetFlow` list particles.ts draws --
 *  one block per request in flight, on the path of the target that is actually
 *  serving it. Nothing is emitted for an idle target and nothing is
 *  synthesised for a busy one.
 *
 *  It draws every band, not the selected one. Scoping it to the selection meant
 *  a graph with nothing clicked drew nothing at all, which is the state it is
 *  in almost all of the time -- and on a cluster whose only traffic goes to a
 *  provider it meant the animation had never once run.
 *
 *  Its own component because it subscribes to the metrics stream, and nothing
 *  else in the graph should re-render because it did. Renders nothing itself. */
function ParticleField({
  layerRef,
  routing,
  paths,
  bands,
}: {
  layerRef: RefObject<SVGGElement>
  routing: RoutingConfig[]
  paths: ClusterLayout['paths']
  bands: ClusterBand[]
}) {
  const { selDep } = useSelection()
  const { frame, stale } = useMetrics()
  const mix = useRequestMix(selDep)

  const deploymentFrames = frame?.deployments
  const flows = useMemo<TargetFlow[]>(
    () => targetFlows(routing, deploymentFrames, stale),
    [routing, deploymentFrames, stale],
  )

  // One ratio for the whole out-stream, over the last fifteen minutes. The
  // window is the honest limit and the legend names it; see `streamTone`.
  const streaming = useMemo(
    () => mostlyStreaming(streamingRatio(mix.data?.requests ?? [])),
    [mix.data],
  )

  const streams = useMemo<StreamFlow[]>(
    () =>
      // Every band that is producing, so a name is not left showing requests
      // arriving and nothing coming back. The RATE is per band and measured --
      // `bandTokensPerSec` reads that band's own figure off the same 1 Hz
      // frame the readout on it does.
      //
      // The COLOUR is not, and is not faked to match. Whether a name was
      // called with `stream: true` lives only in the request archive, which
      // `useRequestMix` queries for ONE served name; asking per band would be
      // a query per band for a hue. So the selected band paints its measured
      // tone and every other passes null, which `streamTone` already renders
      // as the same "no reading" grey an empty meter track uses. Selecting a
      // band is what answers the question for that band.
      bands.map((b) => ({
        pathKey: outFlightKey(b.id),
        // A stale stream emits nothing rather than repainting a rate that
        // stopped arriving -- the same discipline as the greyed readouts.
        tokensPerSec: stale ? 0 : (bandTokensPerSec(b, frame) ?? 0),
        streaming: b.servedName === selDep ? streaming : null,
      })),
    [bands, selDep, frame, stale, streaming],
  )

  useParticleField({ layerRef, flows, streams, paths })
  return null
}
