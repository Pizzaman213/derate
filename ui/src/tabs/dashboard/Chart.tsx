import {
  createContext,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import uPlot from 'uplot'
import 'uplot/dist/uPlot.min.css'
import type { HistoryEnvelope } from '../../api/types'
import type { TelemetryPoint } from '../../state/useTelemetry'
import {
  align,
  bounds,
  gapAt,
  gapSpans,
  kindFor,
  seriesKey,
  yRange,
  type Bounds,
  type Columns,
  type GapSpan,
  type YKind,
} from './chart'
import { readChartTheme, subscribeChartTheme, type ChartTheme } from './chartTheme'

// Two renderings, deliberately by two different means.
//
// `Chart` is uPlot. It was hand-rolled SVG until the archive windows made the
// hand-rolling the limiting factor: a 24-hour window is tens of thousands of
// points, a rolled bucket carries an average AND a maximum that the single
// path could not both draw, and four charts of one machine could not share a
// cursor. uPlot is 45 KB of canvas that does all three, and -- the reason it
// is the right library and Recharts is not -- its `spanGaps` defaults to
// FALSE. A null lifts the pen without being asked, which is the one property
// this codebase cannot trade away. Nothing about the box changed: the title
// row, the value, the min/note/max axis line are the same markup, styled by
// the same `.chart` rules.
//
// `ChartCell` is still SVG, and should stay that way. It is a 24px trace in a
// table row with no axis, no cursor and no band -- none of what uPlot brings
// -- and the deployments strip draws one PER ROW, where a canvas, a DOM
// subtree and a ResizeObserver each would be pure overhead. Both renderers
// take their nulls from the same place: `chart.ts` is the only file that
// decides what a hole is, so the two cannot drift.

const FULL_H = 48
const CELL_H = 24
const CELL_W = 100

/** uPlot syncs cursors by a group key. `ChartGrid` mints one per grid so that
 *  hovering any chart puts the crosshair at the same INSTANT on its siblings
 *  and every readout in the grid answers for that instant -- which is the
 *  whole question a four-up of one machine is asked: what were power,
 *  temperature, utilisation and memory doing at the same moment. Two grids on
 *  one screen stay independent because the keys differ. */
const SyncKey = createContext<string | null>(null)

let syncSeq = 0

export function ChartGrid({ children }: { children: ReactNode }) {
  const key = useMemo(() => `chartgrid-${(syncSeq += 1)}`, [])
  return (
    <SyncKey.Provider value={key}>
      <div className="chartgrid">{children}</div>
    </SyncKey.Provider>
  )
}

interface ChartProps {
  title: string
  unit: string
  points: TelemetryPoint[]
  decimals?: number
  /** What sits between the min and max labels. Defaults to `${n}s`, which is
   *  right only at 1 Hz: a series read back from the archive at 1-minute or
   *  1-hour buckets has the same point count and a completely different span,
   *  and "60s" under an hourly chart is simply false. Callers drawing history
   *  pass `resolutionNote()`. Two labels and a middle note is what the axis
   *  already was -- no chrome is added here. */
  note?: string
  /** The upper edge of a shaded band under which `points` is the lower edge:
   *  a bucket's maximum over its average, or a p99 over a p50. One quantity in
   *  two aggregations, so it is drawn as one hue at two strengths and never as
   *  a second series with a second colour. Absent for a raw window, where
   *  every point IS its own maximum. */
  band?: TelemetryPoint[]
  /** What the band's two edges mean, for the axis line. The lower edge is
   *  `points` itself, so naming it is only necessary once a band exists --
   *  "min 61" under a banded chart is the lowest AVERAGE, not the lowest
   *  reading, and the two are different claims. */
  bandLabel?: string
  lowLabel?: string
  /** Where the numbers came from. Only `gaps` is used here, drawn as hatched
   *  stretches under the trace: a hole the archive KNOWS it is missing and a
   *  machine that was merely quiet are the same flat line otherwise, and
   *  saying so only in the paragraph underneath left the drawing itself
   *  ambiguous. */
  envelope?: HistoryEnvelope | null
  /** Overrides the axis rule inferred from `unit`. See chart.ts. */
  kind?: YKind
  height?: number
}

/** A titled chart: its own min/max, the count of actual samples behind it, and
 *  a crosshair shared with every sibling in its `ChartGrid`. An all-null or
 *  empty series renders "no samples yet" rather than a flat line at zero. */
export function Chart({
  title,
  unit,
  points,
  decimals = 0,
  note,
  band,
  bandLabel = 'peak',
  lowLabel,
  envelope,
  kind,
  height = FULL_H,
}: ChartProps) {
  const syncKey = useContext(SyncKey)
  const b = bounds(points)
  const bandB = band ? bounds(band) : null

  // The axis rule is chosen against BOTH edges: a band whose maximum leaves
  // the top of a range fitted to the average is a band that lies about the
  // peak it exists to show.
  const extent: Bounds | null =
    b && bandB
      ? { mn: Math.min(b.mn, bandB.mn), mx: Math.max(b.mx, bandB.mx), n: b.n, last: b.last }
      : (b ?? bandB)

  // Keyed by content rather than by array identity: `nodeSeries` and
  // `depSeries` build a fresh array on every render, so identity deps would
  // realign -- and push a full canvas redraw -- on every mouse move over the
  // crosshair. See `seriesKey` for exactly what "content" is taken to mean.
  const key = seriesKey(points)
  const bandKey = band ? seriesKey(band) : ''
  const cols = useMemo(
    () => align(band ? [points, band] : [points]),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [key, bandKey],
  )
  const range = useMemo(
    () => yRange(kind ?? kindFor(unit), extent),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [kind, unit, extent?.mn, extent?.mx],
  )
  const spans = useMemo(
    () => gapSpans(envelope, cols[0][0] ?? 0, cols[0][cols[0].length - 1] ?? 0),
    [envelope, cols],
  )

  const [hover, setHover] = useState<number | null>(null)

  if (!b || points.length === 0) {
    return (
      <div className="chart">
        <h4>
          <span>{title}</span>
          <span className="now">—</span>
        </h4>
        <div className="unit">no samples yet</div>
      </div>
    )
  }

  // What the value slot says. Not hovering: the latest reading, as before.
  // Hovering: the reading AT THE CROSSHAIR -- and over a known hole, the
  // archive's own reason for the hole rather than the nearest value from the
  // far side of it, which is the number the old chart would have implied.
  const at = hover != null ? cols[0][hover] : null
  const hole = at != null ? gapAt(spans, at) : null
  const shown = hover != null ? (cols[1]?.[hover] ?? null) : b.last
  const shownBand = hover != null && band ? (cols[2]?.[hover] ?? null) : null

  let readout: ReactNode
  if (hole) {
    readout = <span className="unit">missing · {hole.reason}</span>
  } else if (shown == null) {
    readout = <span className="unit">no sample</span>
  } else {
    readout = (
      <>
        {shown.toFixed(decimals)}
        {shownBand != null ? ` → ${shownBand.toFixed(decimals)}` : ''}{' '}
        <span className="unit">{unit}</span>
      </>
    )
  }

  return (
    <div className="chart">
      <h4>
        <span>{title}</span>
        <span className="now">{readout}</span>
      </h4>
      <Plot
        cols={cols}
        range={range}
        spans={spans}
        hasBand={!!band}
        height={height}
        syncKey={syncKey}
        onHover={setHover}
        label={`${title}${band ? `, average and ${bandLabel}` : ''}`}
      />
      <div className="ax">
        <span>
          min {b.mn.toFixed(decimals)}
          {bandB && lowLabel ? <span className="unit"> {lowLabel}</span> : null}
        </span>
        <span>{hover != null && at != null ? clock(at) : (note ?? `${b.n}s`)}</span>
        <span>
          max {(bandB ? Math.max(b.mx, bandB.mx) : b.mx).toFixed(decimals)}
          {bandB && bandB.mx > b.mx ? <span className="unit"> {bandLabel}</span> : null}
        </span>
      </div>
    </div>
  )
}

interface PlotProps {
  cols: Columns
  range: { min: number; max: number }
  spans: GapSpan[]
  hasBand: boolean
  height: number
  syncKey: string | null
  onHover: (idx: number | null) => void
  label: string
}

/** The uPlot instance and nothing else.
 *
 *  Split from `Chart` on purpose: the readout above re-renders on every mouse
 *  move, and this must not. The instance is built once per (theme, shape,
 *  sync group); data, size, y-range and gap geometry all reach it through refs
 *  and imperative calls, so a hover costs one canvas redraw rather than a
 *  teardown. */
function Plot({ cols, range, spans, hasBand, height, syncKey, onHover, label }: PlotProps) {
  const hostRef = useRef<HTMLDivElement>(null)
  const plotRef = useRef<uPlot | null>(null)
  const rangeRef = useRef(range)
  const spansRef = useRef(spans)
  const hoverRef = useRef(onHover)
  rangeRef.current = range
  spansRef.current = spans
  hoverRef.current = onHover

  const [theme, setTheme] = useState<ChartTheme>(readChartTheme)
  const [width, setWidth] = useState(0)

  useEffect(() => subscribeChartTheme(() => setTheme(readChartTheme())), [])

  // The width is measured rather than assumed because these boxes live in a
  // grid that reflows at 900px, and because the Dashboard keeps its sub-tabs
  // mounted under `hidden` -- a chart built while its tab is hidden is built
  // at zero width, so construction waits for a real one and the observer
  // supplies it the moment the tab is shown.
  useLayoutEffect(() => {
    const host = hostRef.current
    if (!host) return
    const ro = new ResizeObserver((entries) => {
      const w = Math.floor(entries[0]!.contentRect.width)
      if (w > 0) setWidth(w)
    })
    ro.observe(host)
    return () => ro.disconnect()
  }, [])

  useLayoutEffect(() => {
    const host = hostRef.current
    if (!host || width <= 0) return

    const opts: uPlot.Options = {
      width,
      height,
      // No axes and no legend: the box already has a title, a value and a
      // min/note/max line, and adding uPlot's own chrome would say all three
      // twice in a 48px plot. The cursor is what this library was brought in
      // for, not its axis renderer.
      axes: [{ show: false }, { show: false }],
      legend: { show: false },
      padding: [4, 1, 0, 1],
      cursor: {
        // x only. Four charts of one machine are four different units, so a
        // horizontal line at "the same y" would be meaningless across them,
        // while the same instant is exactly what they are read for.
        y: false,
        drag: { x: false, y: false, setScale: false },
        points: { show: true, size: 5 },
        ...(syncKey ? { sync: { key: syncKey, setSeries: false, scales: ['x', null] } } : {}),
      },
      scales: {
        x: { time: true },
        // Read through the ref so new data re-asks the question without the
        // instance being rebuilt. Returning a fixed pair is the point: this is
        // where a percentage stops being scaled to its own noise.
        y: { range: (): [number, number] => [rangeRef.current.min, rangeRef.current.max] },
      },
      series: [
        {},
        {
          stroke: theme.ink,
          width: 1.5,
          points: { show: false },
          // Explicit, though it is also the default. It is the single property
          // that made uPlot the right choice, so it is written down rather
          // than inherited silently.
          spanGaps: false,
        },
        ...(hasBand
          ? [
              {
                stroke: theme.bandEdge,
                width: 0.75,
                points: { show: false },
                spanGaps: false,
              },
            ]
          : []),
      ],
      // [upper, lower]: the bucket maximum over the average it was rolled
      // from. Where either edge is null the fill breaks with the lines, so a
      // hole stays a hole in the band too.
      bands: hasBand ? [{ series: [2, 1] as [number, number], fill: theme.band }] : [],
      hooks: {
        setCursor: [(u) => hoverRef.current(u.cursor.idx ?? null)],
        // Before the series, so a hole is a ground the trace is drawn on
        // rather than a smear over it. A gap is drawn even where it has no
        // samples on either side, which is the case it exists for.
        drawClear: [
          (u) => {
            const g = spansRef.current
            if (g.length === 0) return
            const ctx = u.ctx
            ctx.save()
            ctx.fillStyle = theme.gap
            for (const s of g) {
              const x0 = u.valToPos(s.from, 'x', true)
              const x1 = u.valToPos(s.to, 'x', true)
              ctx.fillRect(x0, u.bbox.top, Math.max(x1 - x0, 1), u.bbox.height)
            }
            ctx.restore()
          },
        ],
      },
    }

    const u = new uPlot(opts, cols as uPlot.AlignedData, host)
    plotRef.current = u
    return () => {
      u.destroy()
      plotRef.current = null
    }
    // Data, size and gaps are pushed imperatively below; rebuilding on any of
    // them would throw away the canvas sixty times a minute.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [width > 0, height, hasBand, syncKey, theme])

  useEffect(() => {
    const u = plotRef.current
    if (!u) return
    // `false`: never reset the scales. The y range is ours and the x range is
    // the window the caller asked for.
    u.setData(cols as uPlot.AlignedData, false)
  }, [cols])

  useEffect(() => {
    const u = plotRef.current
    if (u && width > 0) u.setSize({ width, height })
  }, [width, height])

  useEffect(() => {
    plotRef.current?.redraw(false, false)
  }, [range, spans])

  return <div ref={hostRef} className="plot" role="img" aria-label={label} style={{ height }} />
}

/** The instant under the crosshair, in the reader's own locale. Seconds are
 *  kept: at 1 Hz they are the only thing distinguishing one point from the
 *  next, and under an hourly bucket they are a harmless :00. */
function clock(t: number): string {
  return new Date(t * 1000).toLocaleTimeString()
}

/** The bare trace used in the deployments strip's spark cell: same gap
 *  semantics as `Chart`, none of its chrome -- there is no room for a title or
 *  an axis in a 24px row, and no reason to spend a canvas on one. */
export function ChartCell({ points, label }: { points: TelemetryPoint[]; label: string }) {
  const b = bounds(points)
  if (!b || points.length < 2) {
    return <span className="unit">no samples yet</span>
  }
  const [xs, ys] = align([points])
  const t0 = xs[0]!
  const t1 = xs[xs.length - 1]!
  const dt = t1 - t0 || 1
  const span = b.mx - b.mn || 1

  let d = ''
  let pen = false
  for (let i = 0; i < xs.length; i += 1) {
    const v = ys![i]
    if (v == null) {
      pen = false
      continue
    }
    const x = ((xs[i]! - t0) / dt) * CELL_W
    const y = CELL_H - ((v - b.mn) / span) * (CELL_H - 4) - 2
    d += `${pen ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)} `
    pen = true
  }

  return (
    <svg
      viewBox={`0 0 ${CELL_W} ${CELL_H}`}
      preserveAspectRatio="none"
      style={{ width: '100%', height: CELL_H, display: 'block' }}
      role="img"
      aria-label={label}
    >
      <path
        d={d.trim()}
        fill="none"
        stroke="var(--ink)"
        strokeWidth={1}
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  )
}
