import type { MouseEvent, ReactNode } from 'react'

export type BannerProps = {
  /** `fault` is announced at once (role="alert"); `warn` politely (role="status"). */
  tone: 'fault' | 'warn'
  children: ReactNode
  /** One way forward, as a real link so it can be opened in a second tab. */
  action?: { label: string; href: string; onClick?: (e: MouseEvent<HTMLAnchorElement>) => void }
}

/** A full-width strip under the header for a state the whole app is in.
 *  No dismiss control: it reflects state, so it leaves when the state clears
 *  -- unlike a Toast, which reports an outcome. */
export function Banner({ tone, children, action }: BannerProps) {
  return (
    <div
      role={tone === 'fault' ? 'alert' : 'status'}
      data-tone={tone}
      style={{
        display: 'flex',
        flexWrap: 'wrap',
        alignItems: 'baseline',
        gap: 'var(--space-8)',
        padding: 'var(--space-8) var(--space-16)',
        background: 'var(--panel)',
        borderLeft: `var(--space-3) solid var(--${tone})`,
        color: `var(--${tone})`,
      }}
    >
      <span style={{ flex: '1 1 auto' }}>{children}</span>
      {action ? (
        <a
          href={action.href}
          onClick={action.onClick}
          style={{ display: 'inline-flex', alignItems: 'center', minHeight: 'var(--space-24)', color: 'inherit', textDecoration: 'underline' }}
        >
          {action.label}
        </a>
      ) : null}
    </div>
  )
}
