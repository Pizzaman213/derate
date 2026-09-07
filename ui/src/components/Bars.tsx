export interface Segment {
  key: string
  label: string
  bytes: number
  /** The term the fit gate named as limiting. Marked, not merely coloured. */
  limiting?: boolean
}

interface SegmentBarProps {
  segments: Segment[]
  /** The line the verdict is measured against, and the ONLY one the overrun
   *  test uses. null when nothing measured it -- which draws no fault region
   *  rather than implying everything fits. */
  usable: number | null
  /** A second line drawn behind, for context only: the static ceiling when
   *  `usable` is the live figure. Never used for the overrun test. Two rules
   *  of equal weight read as a range, and repainting past both would put
   *  three colour regions in a bar whose whole thesis is that colour reports
   *  state rather than category. */
  ceiling?: number | null
  height?: number
}

/** Two lines within this share of the span are the same line, visually. */
const COINCIDENT = 0.01

/** The memory breakdown, drawn proportionally against the line the verdict
 *  used. Everything past that line is over budget and is drawn in fault. */
export function SegmentBar({ segments, usable, ceiling, height = 22 }: SegmentBarProps) {
  const total = segments.reduce((a, s) => a + s.bytes, 0)
  // Scale so the total and both lines are always on screen. When the total
  // overruns, the line sits partway across and the overrun is visible.
  const span = Math.max(total, usable ?? 0, ceiling ?? 0) * 1.02
  const pct = (b: number) => `${(b / span) * 100}%`
  // A missing budget is not a budget of zero: no line, and no fault region.
  const overruns = usable != null && total > usable
  // Suppress a ceiling at or below the live line, or close enough to it that
  // two rules would render as a smudge.
  const showCeiling =
    ceiling != null &&
    usable != null &&
    ceiling > usable &&
    (ceiling - usable) / span > COINCIDENT

  let run = 0
  return (
    <div>
      <div
        style={{
          position: 'relative',
          height,
          background: 'var(--panel-recessed)',
          border: '1px solid var(--rule)',
        }}
      >
        {segments.map((s, i) => {
          const left = pct(run)
          run += s.bytes
          const beyond = usable != null && run > usable
          return (
            <div
              key={s.key}
              title={`${s.label}${s.limiting ? ' (limiting)' : ''}`}
              style={{
                position: 'absolute',
                left,
                width: pct(s.bytes),
                top: 0,
                bottom: 0,
                background:
                  overruns && beyond ? 'var(--fault)' : 'var(--ink)',
                opacity: shade(i, segments.length),
                borderRight: '1px solid var(--panel)',
                // The term the fit gate named is outlined, not recoloured:
                // colour in this interface reports state, not category.
                outline: s.limiting ? '1.5px solid var(--ink)' : undefined,
                outlineOffset: s.limiting ? '-1.5px' : undefined,
              }}
            />
          )
        })}
        {/* The static ceiling, behind the blocks: a hairline the segments
            paint over reads as "the old ceiling, now covered", which is what
            it is. Weight and dash separate the two lines, never hue. */}
        {showCeiling ? (
          <div
            style={{
              position: 'absolute',
              left: pct(ceiling as number),
              top: 0,
              bottom: 0,
              width: 0,
              borderLeft: '1px dashed var(--ink-muted)',
              zIndex: 0,
            }}
            title="ceiling on idle hardware"
          />
        ) : null}
        {/* The line the verdict actually used. */}
        {usable != null ? (
          <div
            style={{
              position: 'absolute',
              left: pct(usable),
              top: -4,
              bottom: -4,
              width: 0,
              borderLeft: '2px solid var(--ink)',
              zIndex: 1,
            }}
            title="the memory this verdict was measured against"
          />
        ) : null}
      </div>
    </div>
  )
}

/** Segments are one colour at descending weight. Memory terms are not
 *  categories that deserve colour; colour is reserved for state. */
function shade(i: number, n: number): number {
  if (n <= 1) return 0.85
  return 0.85 - (i / (n - 1)) * 0.55
}

interface ProportionProps {
  /** 0..1, or null when there is no reading -- a null renders a dashed
   *  outline with NO fill, visually distinct from a genuine 0% (empty but
   *  solid track). A missing value must never be pixel-identical to zero. */
  value: number | null
  width?: number | string
  height?: number
  tone?: 'ink' | 'live' | 'warn' | 'fault' | 'muted'
  label: string
}

/** A single proportion. Used for a node's memory fill and a routing share, so
 *  an unequal split is visible rather than mysterious. */
export function ProportionBar({
  value,
  width = '100%',
  height = 4,
  tone = 'ink',
  label,
}: ProportionProps) {
  const color =
    tone === 'ink'
      ? 'var(--ink)'
      : tone === 'muted'
        ? 'var(--ink-muted)'
        : `var(--${tone})`
  return (
    <div
      role="img"
      aria-label={label}
      title={label}
      style={{
        width,
        height,
        background: value == null ? 'transparent' : 'var(--panel-recessed)',
        border: value == null ? '1px dashed var(--rule)' : '1px solid var(--rule)',
        position: 'relative',
      }}
    >
      {value != null ? (
        <div
          style={{
            position: 'absolute',
            inset: 0,
            right: `${(1 - Math.max(0, Math.min(1, value))) * 100}%`,
            background: color,
          }}
        />
      ) : null}
    </div>
  )
}
