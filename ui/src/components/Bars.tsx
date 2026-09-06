export interface Segment {
  key: string
  label: string
  bytes: number
  /** The term the fit gate named as limiting. Marked, not merely coloured. */
  limiting?: boolean
}

interface SegmentBarProps {
  segments: Segment[]
  /** The usable-memory line the total is measured against. */
  usable: number
  height?: number
}

/** The memory breakdown, drawn proportionally against the usable line.
 *  Everything past the line is over budget and is drawn in fault. */
export function SegmentBar({ segments, usable, height = 22 }: SegmentBarProps) {
  const total = segments.reduce((a, s) => a + s.bytes, 0)
  // Scale so both the total and the usable line are always on screen. When the
  // total overruns, the line sits partway across and the overrun is visible.
  const span = Math.max(total, usable) * 1.02
  const pct = (b: number) => `${(b / span) * 100}%`
  const overruns = total > usable

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
          const beyond = run > usable
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
        {/* the usable line */}
        <div
          style={{
            position: 'absolute',
            left: pct(usable),
            top: -4,
            bottom: -4,
            width: 0,
            borderLeft: '2px solid var(--ink)',
          }}
          title="usable memory per node"
        />
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
  /** 0..1 */
  value: number
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
        background: 'var(--panel-recessed)',
        border: '1px solid var(--rule)',
        position: 'relative',
      }}
    >
      <div
        style={{
          position: 'absolute',
          inset: 0,
          right: `${(1 - Math.max(0, Math.min(1, value))) * 100}%`,
          background: color,
        }}
      />
    </div>
  )
}
