import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
  type RefObject,
} from 'react'
import type {
  DeploymentDTO,
  NodeStateDTO,
  RoutingConfig,
  Topology,
  TopologyNode,
} from '../../api/types'
import { useSelection } from '../../state/selection'
import { useMetrics } from '../../state/metrics'
import { nodeLive, nodeSignal } from '../../state/live'
import { PROPORTIONAL } from '../../state/policy'
import { fmt, gbytes, pct, shortGpu } from '../../format'
import {
  layoutCluster,
  moveToSlot,
  nearestSlot,
  plateWidth,
  type ClusterBand,
  type ClusterEdge,
  type ClusterLayout,
  type PlacedCard,
  type Point,
} from './layout'
import { useParticleField } from './particles'

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
  zoomLabelRef: RefObject<HTMLSpanElement>
  order: string[] | null
  onReorder: (order: string[]) => void
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
  { deployments, topology, nodes, routing, zoomLabelRef, order, onReorder, announce },
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

  const layout = useMemo(
    () =>
      layoutCluster({
        nodes: topology.nodes,
        links: topology.edges,
        deployments,
        routing,
        selection: { selDep, selNode, selLink },
        width: box.w,
        height: box.h,
        order,
      }),
    [topology, deployments, routing, selDep, selNode, selLink, box, order],
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
  // What is persisted is a SLOT PERMUTATION, not a free x/y. Free coordinates
  // stop meaning anything the moment the viewport resizes or the cluster
  // crosses a density tier and the plate size changes, and they let a plate sit
  // on top of a link. A permutation survives both and keeps the floor composed,
  // while still letting someone arrange the machines to match the rack.
  const cardDrag = useRef<{ nodeId: string; from: Point; moved: boolean } | null>(null)
  /** The last press on a plate that did not become a drag: the first half of a
   *  double-click, waiting to see whether a second one lands on the same
   *  machine inside DOUBLE_MS. */
  const lastPress = useRef<{ nodeId: string; at: number } | null>(null)
  const ghostRef = useRef<SVGGElement>(null)
  const [dropSlot, setDropSlot] = useState<number | null>(null)
  const [dragging, setDragging] = useState<string | null>(null)

  const commitOrder = (nodeId: string, slot: number, spoken: string) => {
    const next = moveToSlot(layout.arrangement, nodeId, slot)
    if (next !== layout.arrangement) {
      onReorder(next)
      announce(spoken)
    }
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
      const plate = (e.target as Element).closest?.('.machine')
      if (plate) {
        const nodeId = plate.getAttribute('data-node')
        if (nodeId) {
          cardDrag.current = { nodeId, from: toScene(e), moved: false }
          svg.setPointerCapture(e.pointerId)
          return
        }
      }
      if ((e.target as Element).closest?.('.edge,.band')) return
      drag.current = { x: e.clientX, y: e.clientY, tx: view.current.tx, ty: view.current.ty }
      svg.setPointerCapture(e.pointerId)
      svg.style.cursor = 'grabbing'
    }

    const onPointerMove = (e: PointerEvent) => {
      const cd = cardDrag.current
      if (cd) {
        const p = toScene(e)
        const dx = p.x - cd.from.x
        const dy = p.y - cd.from.y
        if (!cd.moved && Math.hypot(dx, dy) < DRAG_SLOP) return
        if (!cd.moved) {
          cd.moved = true
          setDragging(cd.nodeId)
        }
        ghostRef.current?.setAttribute('transform', `translate(${dx.toFixed(1)} ${dy.toFixed(1)})`)
        const plate = layout.cards.find((c) => c.nodeId === cd.nodeId)
        if (plate) {
          const at = { x: plate.x + plate.w / 2 + dx, y: plate.y + plate.h / 2 + dy }
          setDropSlot(nearestSlot(layout.slots, layout.card, at))
        }
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
        const slot = dropSlot
        setDragging(null)
        setDropSlot(null)
        if (cd.moved && slot != null) {
          lastPress.current = null
          commitOrder(cd.nodeId, slot, `${cd.nodeId} moved to position ${slot + 1}.`)
        } else if (!cd.moved) {
          const at = performance.now()
          const prev = lastPress.current
          if (prev && prev.nodeId === cd.nodeId && at - prev.at <= DOUBLE_MS) {
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
      if (e.altKey && arrows[e.key]) {
        const focused = (e.target as Element)?.closest?.('.machine')?.getAttribute('data-node')
        if (focused) {
          e.preventDefault()
          const at = layout.arrangement.indexOf(focused)
          const cols = layout.cards.filter((cd) => cd.row === 0).length || 1
          const delta =
            e.key === 'ArrowLeft' ? -1 : e.key === 'ArrowRight' ? 1 : e.key === 'ArrowUp' ? -cols : cols
          const to = Math.max(0, Math.min(layout.arrangement.length - 1, at + delta))
          commitOrder(focused, to, `${focused} moved to position ${to + 1} of ${layout.arrangement.length}.`)
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
  }, [layout, topology.edges, selLink, selectLink, selectNode, openSheet, dropSlot, onReorder, announce])

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

  return (
    <div ref={hostRef} style={{ flex: 1, minHeight: 0 }}>
      <svg
        ref={svgRef}
        viewBox={`0 0 ${layout.width} ${layout.height}`}
        style={{ width: '100%', height: '100%', display: 'block' }}
        tabIndex={0}
        aria-label={`Cluster graph: ${layout.cards.length} machines, ${layout.edges.length} links drawn. Drag to pan, scroll to zoom. Drag a machine to rearrange it, or hold alt and press an arrow key.`}
      >
        <g ref={sceneRef} id="scene">
          {/* The whole drawing, slid right so its ink sits in the middle of
              the viewBox (see layout.offsetX). It is inside the pan/zoom
              scene, so panning and zooming are unaffected, and it is a
              constant, so the drag maths -- which works in deltas against
              layout coordinates -- does not have to undo it. */}
          <g transform={`translate(${layout.offsetX} 0)`}>
            {/* Drop targets sit under everything, so a plate can be dropped on
                the slot of the machine it is swapping with. */}
            {dragging
              ? layout.slots.map((s, i) => (
                  <rect
                    key={i}
                    x={s.x - 3}
                    y={s.y - 3}
                    width={layout.card.w + 6}
                    height={layout.card.h + 6}
                    rx={6}
                    fill={i === dropSlot ? 'var(--hover)' : 'none'}
                    stroke={i === dropSlot ? 'var(--select-on-panel)' : 'var(--rule)'}
                    strokeWidth={i === dropSlot ? 1.5 : 1}
                    strokeDasharray={i === dropSlot ? undefined : '3 3'}
                  />
                ))
              : null}

            {/* Flow furniture: flat square-cap runs for the measured on-premises
                path, butt-cap dashes for anything crossing the routing boundary
                where there is no measured fabric to draw a figure from. */}
            {layout.conns.map((c) => (
              <path
                key={c.id}
                d={c.d}
                fill="none"
                stroke="var(--ink)"
                strokeWidth={c.weight}
                strokeDasharray={c.dashed ? '5 4' : undefined}
                strokeLinecap={c.dashed ? 'butt' : 'square'}
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

            {layout.entry ? <EntryPlate box={layout.entry} /> : null}

            {layout.bands.map((b) => (
              <Band
                key={b.deploymentId}
                band={b}
                onSelect={selectDep}
                onOpen={(name) => openSheet({ kind: 'dep', id: name })}
              />
            ))}

            <PlateLayer
              layout={layout}
              topologyNodes={topology.nodes}
              nodes={nodes}
              deployments={deployments}
              routing={routing}
              dragging={dragging}
              ghostRef={ghostRef}
              onSelect={selectNode}
              onOpen={(id) => openSheet({ kind: 'node', id })}
            />

            {layout.boundaryY != null && layout.provider ? (
              <Boundary
                y={layout.boundaryY}
                x={layout.provider.x}
                /* The rule spans the viewBox, not the drawing, so it has to
                   undo the centring translate its group carries. */
                from={-layout.offsetX}
                to={layout.width - layout.offsetX}
              />
            ) : null}
            {layout.provider ? <ProviderBus provider={layout.provider} /> : null}
          </g>
        </g>
        <ParticleField
          layerRef={particleLayerRef}
          deployments={deployments}
          routing={routing}
          paths={layout.paths}
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
          {/* A fat transparent hit area, so a 1-unit line is still clickable. */}
          <path d={edge.d} fill="none" stroke="transparent" strokeWidth={12} />
          <path
            d={edge.d}
            fill="none"
            stroke={edge.measured ? 'var(--ink)' : 'var(--ink-muted)'}
            strokeWidth={edge.selected ? edge.width + 1 : edge.width}
            strokeDasharray={edge.dashed ? '4 4' : undefined}
            opacity={edge.opacity}
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

function EntryPlate({ box }: { box: { x: number; y: number; w: number; h: number } }) {
  return (
    <g>
      <rect x={box.x} y={box.y} width={box.w} height={box.h} rx={4} fill="var(--fill)" />
      {/* One line, not two: the endpoint's path is the label, and the second
          line is deliberately gone. Baseline is the box's own middle so
          dropping it does not leave the remaining line sitting high. */}
      <text x={box.x + 12} y={box.y + box.h / 2 + 4} className="m" fontSize={10} fill="var(--on-fill)">
        POST /v1/chat/
      </text>
    </g>
  )
}

const BOUNDARY_LABEL = 'routing boundary · managed, never sharded'

function Boundary({ y, x, from, to }: { y: number; x: number; from: number; to: number }) {
  return (
    <g>
      <line x1={from} y1={y} x2={to} y2={y} stroke="var(--rule)" />
      {/* Knocked through the rule, not laid on top of it. */}
      <rect x={x - 4} y={y - 9} width={plateWidth(BOUNDARY_LABEL, false)} height={18} fill="var(--panel)" />
      <text x={x} y={y + 4} fontSize={11} fill="var(--ink-muted)">
        {BOUNDARY_LABEL}
      </text>
    </g>
  )
}

/** A provider is ONE endpoint that can serve anything, so it is a bus, not a
 *  machine: outlined rather than filled, no telemetry, not interactive. The
 *  brief is emphatic that no provider is ever drawn as a machine, and the only
 *  unfilled box in the drawing is exactly what "not yours" looks like. */
function ProviderBus({ provider }: { provider: NonNullable<ClusterLayout['provider']> }) {
  return (
    <g>
      {/* The half-unit offsets snap a 1-unit stroke onto the grid. */}
      <rect
        x={provider.x + 0.5}
        y={provider.y + 0.5}
        width={provider.w - 1}
        height={provider.h}
        rx={3}
        fill="none"
        stroke="var(--ink)"
        strokeWidth={1}
        opacity={provider.active ? 1 : 0.55}
      />
      {/* Opacity is on the rect, never the group: a .55 group put this box's
          primary line at 3.70:1. The words carry their own colour. */}
      <text
        x={provider.x + 11}
        y={provider.y + 15}
        className="m"
        fontSize={9}
        fill={provider.active ? 'var(--ink)' : 'var(--ink-muted)'}
      >
        {provider.label}
      </text>
      <text x={provider.x + 11} y={provider.y + 30} className="m" fontSize={9} fill="var(--ink-muted)">
        {provider.sublabel}
      </text>
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
  onSelect,
  onOpen,
}: {
  band: ClusterBand
  onSelect: (name: string) => void
  onOpen: (name: string) => void
}) {
  const stroke = band.degraded ? 'var(--warn-solid)' : 'var(--rule)'
  const label = `${band.servedName}${band.plan ? `, ${band.plan}` : ''}, on ${band.members.join(' and ')}`
  return (
    <g
      className="band"
      role="button"
      tabIndex={0}
      aria-label={label}
      onClick={() => onSelect(band.servedName)}
      onDoubleClick={() => onOpen(band.servedName)}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          onSelect(band.servedName)
        }
      }}
    >
      <title>{label}</title>
      <rect className="ring" x={band.x - 3} y={band.y - 3} width={band.w + 6} height={band.h + 6} rx={5} fill="none" />
      {band.leads.map((l, i) => (
        <path key={i} d={`M${l.x} ${l.y1} L${l.x} ${l.y2}`} stroke="var(--rule)" strokeWidth={1} fill="none" />
      ))}
      <rect
        x={band.x}
        y={band.y}
        width={band.w}
        height={band.h}
        rx={3}
        fill="var(--fill)"
        stroke={stroke}
        strokeWidth={1}
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
      <text x={band.x + 11} y={band.y + 15} className="m" fontSize={10} fill="var(--on-fill)">
        {band.servedName}
      </text>
      <text x={band.x + 11} y={band.y + 29} className="m" fontSize={9} fill="var(--on-fill-dim)">
        {[band.plan, band.sublabel, band.degraded ? 'degraded' : ''].filter(Boolean).join(' · ')}
      </text>
      <BandThroughput band={band} />
    </g>
  )
}

/** Its own component so a 1 Hz frame re-renders the number and not the band it
 *  sits on. */
function BandThroughput({ band }: { band: ClusterBand }) {
  const { frame, stale } = useMetrics()
  const tps = frame?.deployments.find((d) => d.deployment_id === band.deploymentId)?.tokens_per_sec
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
  const hostname = topo?.hostname || card.nodeId
  const occupant = servedNames.length
    ? `${servedNames[0]}${servedNames.length > 1 ? ` +${servedNames.length - 1}` : ''}`
    : 'free'

  const tier = card.bodyTier
  const trackW = card.w - 22
  const label = `${hostname}, ${topo?.role ?? 'machine'}, ${topo?.state ?? 'unknown'}, memory ${pct(mem)} percent, position ${card.slot + 1}`

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
        {hostname}
      </text>
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
          y={card.y + 21}
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
          <rect x={card.x + 11} y={card.y + 21} width={trackW} height={14} rx={2} fill="var(--on-fill)" opacity={0.18} />
          <rect
            x={card.x + 11}
            y={card.y + 21}
            width={(trackW * mem) / 100}
            height={14}
            rx={2}
            fill="var(--on-fill)"
            opacity={0.85}
          />
        </>
      )}

      {tier !== 'chip' ? (
        <text x={card.x + 11} y={card.y + 47} className="m" fontSize={9} fill="var(--on-fill-dim)">
          {`${fmt(live?.power_w ?? topo?.power_w, 0)} W · ${fmt(live?.temp_c ?? topo?.temp_c, 0)} °C`}
        </text>
      ) : null}

      {tier === 'full' ? (
        <>
          <text x={card.x + 11} y={card.y + 61} className="m" fontSize={9} fill="var(--on-fill-dim)">
            {`GPU ${fmt(live?.util_pct ?? topo?.util_pct, 0)}% · ${pct(mem)}% memory`}
          </text>
          {shareOf ? (
            <ShareBar card={card} trackW={trackW} share={shareOf} />
          ) : (
            <text x={card.x + 11} y={card.y + 74} className="m" fontSize={9} fill="var(--on-fill-dim)">
              {[shortGpu(topo?.gpu_name ?? '') || topo?.device_class, topo?.total_memory ? `${gbytes(topo.total_memory, 0)} GiB` : '']
                .filter(Boolean)
                .join(' · ')}
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
  share: shareOf,
}: {
  card: PlacedCard
  trackW: number
  share: { served_name: string; weight: number }
}) {
  const w = trackW - 30
  const clamped = Math.max(0, Math.min(1, shareOf.weight))
  return (
    <g>
      <title>{`${shareOf.served_name}: ${pct(shareOf.weight * 100)} percent of traffic`}</title>
      <rect x={card.x + 11} y={card.y + 68} width={w} height={6} rx={2} fill="var(--on-fill)" opacity={0.18} />
      <rect x={card.x + 11} y={card.y + 68} width={w * clamped} height={6} rx={2} fill="var(--on-fill)" opacity={0.85} />
      <text
        x={card.x + card.w - 11}
        y={card.y + 74}
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

/** The particle emitter for the selected deployment, gated on real traffic
 *  (frame tokens/sec or wire-reported outstanding requests). Its own component
 *  because it subscribes to the metrics stream, and nothing else in the graph
 *  should re-render because it did. Renders nothing itself. */
function ParticleField({
  layerRef,
  deployments,
  routing,
  paths,
}: {
  layerRef: RefObject<SVGGElement>
  deployments: DeploymentDTO[]
  routing: RoutingConfig[]
  paths: ClusterLayout['paths']
}) {
  const { selDep } = useSelection()
  const { frame, stale } = useMetrics()

  const cfg = routing.find((c) => c.served_name === selDep) ?? null
  const depIds = deployments.filter((d) => d.served_name === selDep).map((d) => d.deployment_id)
  const tps = (frame?.deployments ?? [])
    .filter((d) => depIds.includes(d.deployment_id))
    .reduce((a, d) => a + (d.tokens_per_sec ?? 0), 0)
  const outstanding = cfg?.targets.reduce((a, t) => a + (t.outstanding || 0), 0) ?? 0
  const hasTraffic = !stale && (tps > 0 || outstanding > 0)

  useParticleField({ layerRef, hasTraffic, servedName: selDep, cfg, paths })
  return null
}
