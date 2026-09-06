type Signal = 'live' | 'warn' | 'fault' | 'idle'

interface Props {
  signal: Signal
  /** Hollow means we are not currently hearing from the source. The lamp stops
   *  claiming the state is fresh without pretending the state changed. */
  hollow?: boolean
  label: string
  size?: number
}

const COLOR: Record<Signal, string> = {
  live: 'var(--live)',
  warn: 'var(--warn)',
  fault: 'var(--fault)',
  idle: 'var(--ink-muted)',
}

/** The one indicator lamp. If it is coloured, something is actually true. */
export function Lamp({ signal, hollow = false, label, size = 8 }: Props) {
  const c = COLOR[signal]
  return (
    <span
      role="img"
      aria-label={label}
      title={label}
      style={{
        display: 'inline-block',
        width: size,
        height: size,
        borderRadius: '50%',
        background: hollow ? 'transparent' : c,
        boxShadow: hollow ? `inset 0 0 0 1.5px ${c}` : 'none',
        flex: '0 0 auto',
      }}
    />
  )
}
