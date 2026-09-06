import type { CSSProperties } from 'react'
import { fmt } from '../format'

interface Props {
  value: number | null | undefined
  decimals?: number
  unit?: string
  /** Characters of width to reserve. Mono plus tabular figures makes 1ch exact,
   *  so reserving the widest expected value means the digits never move and
   *  nothing after them reflows. This is the whole reason for the mono face. */
  width: number
  size?: 'xl' | 'readout' | 'body'
  align?: 'left' | 'right'
  /** The stream is down. Grey the digits and keep the last reading, rather than
   *  freezing a live-looking number or zeroing it. */
  stale?: boolean
  tone?: 'ink' | 'live' | 'warn' | 'fault' | 'muted'
  title?: string
}

const TONE: Record<string, string> = {
  ink: 'var(--ink)',
  live: 'var(--live)',
  warn: 'var(--warn)',
  fault: 'var(--fault)',
  muted: 'var(--ink-muted)',
}

export function Readout({
  value,
  decimals = 0,
  unit,
  width,
  size = 'body',
  align = 'right',
  stale = false,
  tone = 'ink',
  title,
}: Props) {
  const cls = size === 'xl' ? 'readout-xl' : size === 'readout' ? 'readout' : 'mono'
  const style: CSSProperties = {
    display: 'inline-block',
    minWidth: `${width}ch`,
    textAlign: align,
    color: stale ? 'var(--ink-muted)' : TONE[tone],
    fontVariantNumeric: 'tabular-nums',
  }
  return (
    <span title={title}>
      <span className={cls} style={style}>
        {fmt(value, decimals)}
      </span>
      {unit ? <span className="unit" style={{ marginLeft: 4 }}>{unit}</span> : null}
    </span>
  )
}
