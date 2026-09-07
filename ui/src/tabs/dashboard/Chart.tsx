import type { TelemetryPoint } from '../../state/useTelemetry'

// Ported from mockups-next/js/dashboard.js `chart()`. Two renderings share one
// path builder: the titled `Chart` (cluster/per-Spark/per-deployment, in
// TelemetrySub) and the chrome-less `ChartCell` (the deployments strip's 60s
// spark cell). This is also why components/Sparkline.tsx is gone -- see
// DashboardTab's sibling note. Sparkline's points are never-null {t,v}[] and
// breaks a path on elapsed time; a `TelemetryPoint`'s `v: number | null` is a
// real sample ("this tick had nothing to say"), and the whole point of this
// file is that a null breaks the line instead of being interpolated across.

const FULL_W = 100
const FULL_H = 34
const CELL_H = 24

interface Bounds {
  values: number[]
  mn: number
  mx: number
  span: number
}

function bounds(points: TelemetryPoint[]): Bounds | null {
  const values: number[] = []
  for (const p of points) {
    if (p.v != null && Number.isFinite(p.v)) values.push(p.v)
  }
  if (values.length === 0) return null
  const mn = Math.min(...values)
  const mx = Math.max(...values)
  return { values, mn, mx, span: mx - mn || 1 }
}

/** `M`/`L` path across the window, keyed by each point's own timestamp (not
 *  its index) so an irregular gap draws as the gap it is rather than an
 *  evenly-spaced guess. A null value lifts the pen; the next real value
 *  starts a fresh subpath instead of joining across the hole. */
function buildPath(
  points: TelemetryPoint[],
  width: number,
  height: number,
  b: Bounds,
  t0: number,
  t1: number,
): string {
  const dt = t1 - t0 || 1
  let d = ''
  let pen = false
  for (const p of points) {
    if (p.v == null || !Number.isFinite(p.v)) {
      pen = false
      continue
    }
    const x = ((p.t - t0) / dt) * width
    const y = height - ((p.v - b.mn) / b.span) * (height - 4) - 2
    d += `${pen ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)} `
    pen = true
  }
  return d.trim()
}

interface ChartProps {
  title: string
  unit: string
  points: TelemetryPoint[]
  decimals?: number
}

/** A titled chart: its own min/max, a visible baseline, and the count of
 *  actual samples behind it (not a fixed "60s" -- the window starts empty and
 *  fills in). An all-null or empty series renders "no samples yet" rather
 *  than a flat line at zero. */
export function Chart({ title, unit, points, decimals = 0 }: ChartProps) {
  const b = bounds(points)
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

  const t0 = points[0]!.t
  const t1 = points[points.length - 1]!.t
  const d = buildPath(points, FULL_W, FULL_H, b, t0, t1)
  const now = b.values[b.values.length - 1]!

  return (
    <div className="chart">
      <h4>
        <span>{title}</span>
        <span className="now">
          {now.toFixed(decimals)} <span className="unit">{unit}</span>
        </span>
      </h4>
      <svg
        viewBox={`0 0 ${FULL_W} ${FULL_H}`}
        preserveAspectRatio="none"
        style={{ width: '100%', height: 44, display: 'block' }}
      >
        <line
          x1={0}
          y1={FULL_H - 2}
          x2={FULL_W}
          y2={FULL_H - 2}
          stroke="var(--rule)"
          strokeWidth={0.5}
          vectorEffect="non-scaling-stroke"
        />
        <path d={d} fill="none" stroke="var(--ink)" strokeWidth={1} vectorEffect="non-scaling-stroke" />
      </svg>
      <div className="ax">
        <span>min {b.mn.toFixed(decimals)}</span>
        <span>{b.values.length}s</span>
        <span>max {b.mx.toFixed(decimals)}</span>
      </div>
    </div>
  )
}

/** The bare trace used in the deployments strip's spark cell: same gap
 *  semantics as `Chart`, none of its chrome -- there is no room for a title or
 *  an axis in a 24px row. */
export function ChartCell({ points, label }: { points: TelemetryPoint[]; label: string }) {
  const b = bounds(points)
  if (!b || points.length < 2) {
    return <span className="unit">no samples yet</span>
  }
  const t0 = points[0]!.t
  const t1 = points[points.length - 1]!.t
  const d = buildPath(points, FULL_W, CELL_H, b, t0, t1)
  return (
    <svg
      viewBox={`0 0 ${FULL_W} ${CELL_H}`}
      preserveAspectRatio="none"
      style={{ width: '100%', height: CELL_H, display: 'block' }}
      role="img"
      aria-label={label}
    >
      <path d={d} fill="none" stroke="var(--ink)" strokeWidth={1} vectorEffect="non-scaling-stroke" />
    </svg>
  )
}
