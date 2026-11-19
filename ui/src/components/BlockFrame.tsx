import type { ReactNode } from 'react'
import { Icon, type IconName } from './Icon'
import { Lamp } from './Lamp'

export type BlockFrameProps = {
  /** The block's title; also its accessible name. */
  title: string
  /** A second line under the title: what the block is, in words. */
  sub?: string
  icon?: IconName
  /** A lamp beside the title, when the block stands for something live. */
  signal?: { value: 'live' | 'warn' | 'fault' | 'idle'; label: string }
  /** Right-aligned header content: a count, a chip, a checkbox. */
  meta?: ReactNode
  selected?: boolean
  /** Amber = worth a look, red = broken. Nothing else colours a block. */
  tone?: 'warn' | 'fault' | null
  /** A remove control in the header, for blocks the operator added. */
  onRemove?: () => void
  removeLabel?: string
  children?: ReactNode
  footer?: ReactNode
  className?: string
}

const BORDER = {
  fault: 'var(--graph-fault)',
  warn: 'var(--graph-warn)',
} as const

/** The chrome every wiring-editor block shares: a titled panel whose border
 *  carries selection and, when something is wrong, the fault or warn colour.
 *  No React Flow in here -- the sockets are the caller's -- so it renders in
 *  the gallery and anywhere else on its own. */
export function BlockFrame({
  title,
  sub,
  icon,
  signal,
  meta,
  selected = false,
  tone = null,
  onRemove,
  removeLabel,
  children,
  footer,
  className,
}: BlockFrameProps) {
  const border = tone ? BORDER[tone] : selected ? 'var(--graph-block-selected)' : 'var(--graph-block-border)'
  return (
    <div
      className={['blockframe', className].filter(Boolean).join(' ')}
      role="group"
      aria-label={title}
      data-selected={selected || undefined}
      data-tone={tone ?? undefined}
      style={{
        display: 'grid',
        gap: 'var(--space-8)',
        padding: 'var(--space-8) var(--space-12)',
        border: `1px solid ${border}`,
        boxShadow: selected || tone ? `0 0 0 1px ${border}` : 'none',
        borderRadius: 'var(--radius)',
        background: 'var(--graph-block-bg)',
        color: 'var(--ink)',
        minWidth: 0,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-6)', minWidth: 0 }}>
        {icon ? <Icon name={icon} size={14} decorative /> : null}
        {signal ? <Lamp signal={signal.value} label={signal.label} /> : null}
        <span style={{ display: 'grid', minWidth: 0, flex: 1 }}>
          <span className="label" style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={title}>
            {title}
          </span>
          {sub ? <span className="unit">{sub}</span> : null}
        </span>
        {meta}
        {onRemove ? (
          <button type="button" className="ghost nodrag" onClick={onRemove} aria-label={removeLabel ?? `Remove ${title}`} title={removeLabel ?? `Remove ${title}`}>
            <Icon name="x-circle" size={14} decorative />
          </button>
        ) : null}
      </div>
      {children}
      {footer ? <div style={{ borderTop: '1px solid var(--rule)', paddingTop: 'var(--space-6)' }}>{footer}</div> : null}
    </div>
  )
}
