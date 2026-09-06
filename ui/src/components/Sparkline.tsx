import { useEffect, useRef, useState } from 'react'

interface Props {
  points: { t: number; v: number }[]
  /** Seconds the x axis spans. Points older than this are already trimmed. */
  windowSeconds: number
  height?: number
  /** Grey the trace while the stream is down. */
  stale?: boolean
  label: string
}

/** A plain line on a hairline baseline. No fill, no gridlines, no axis chrome
 *  beyond the two end labels, and no charting library for one polyline. */
export function Sparkline({
  points,
  windowSeconds,
  height = 96,
  stale = false,
  label,
}: Props) {
  const hostRef = useRef<HTMLDivElement>(null)
  const [width, setWidth] = useState(0)

  useEffect(() => {
    const el = hostRef.current
    if (!el) return
    const ro = new ResizeObserver(([entry]) => {
      if (entry) setWidth(Math.floor(entry.contentRect.width))
    })
    ro.observe(el)
    setWidth(Math.floor(el.getBoundingClientRect().width))
    return () => ro.disconnect()
  }, [])

  const stroke = stale ? 'var(--ink-muted)' : 'var(--live)'

  return (
    <div>
      <div ref={hostRef} style={{ width: '100%' }}>
        {width > 0 ? (
          <svg
            width={width}
            height={height}
            role="img"
            aria-label={label}
            style={{ display: 'block' }}
          >
            <path
              d={tracePath(points, width, height, windowSeconds)}
              fill="none"
              stroke={stroke}
              strokeWidth={1.5}
              strokeLinejoin="round"
              strokeLinecap="round"
              vectorEffect="non-scaling-stroke"
            />
            {/* the hairline baseline */}
            <line
              x1={0}
              y1={height - 0.5}
              x2={width}
              y2={height - 0.5}
              stroke="var(--rule)"
              strokeWidth={1}
            />
          </svg>
        ) : (
          <div style={{ height }} />
        )}
      </div>
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          marginTop: 6,
        }}
      >
        <span className="unit">{windowSeconds} s ago</span>
        <span className="unit">now</span>
      </div>
    </div>
  )
}

/** Builds the polyline. A gap longer than two sample intervals breaks the path
 *  instead of drawing a straight line across it, so a stream drop reads as
 *  missing data rather than as a flat period of real measurement. */
function tracePath(
  points: { t: number; v: number }[],
  width: number,
  height: number,
  windowSeconds: number,
): string {
  if (points.length < 2) return ''

  const last = points[points.length - 1]!
  const t1 = last.t
  const t0 = t1 - windowSeconds

  const values = points.map((p) => p.v)
  let lo = Math.min(...values)
  let hi = Math.max(...values)
  // Quantise the domain so the trace does not rescale on every frame. A line
  // that rewrites its own y axis once a second is unreadable.
  const step = niceStep(hi - lo)
  lo = Math.floor(lo / step) * step - step
  hi = Math.ceil(hi / step) * step + step
  if (hi - lo < 1e-6) hi = lo + 1

  const pad = 3
  const x = (t: number) => ((t - t0) / windowSeconds) * width
  const y = (v: number) =>
    height - pad - ((v - lo) / (hi - lo)) * (height - pad * 2)

  const gapThreshold = 2.5
  let d = ''
  let prevT: number | null = null
  for (const p of points) {
    const cmd = prevT === null || p.t - prevT > gapThreshold ? 'M' : 'L'
    d += `${cmd}${x(p.t).toFixed(2)} ${y(p.v).toFixed(2)} `
    prevT = p.t
  }
  return d.trim()
}

function niceStep(range: number): number {
  if (range <= 0) return 1
  const mag = 10 ** Math.floor(Math.log10(range))
  const norm = range / mag
  const mult = norm < 1.5 ? 0.2 : norm < 3 ? 0.5 : norm < 7 ? 1 : 2
  return mult * mag
}
