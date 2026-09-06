import type { ReactNode } from 'react'

/** A sidebar section: a printed legend, a hairline, then content. Panels are
 *  separated by rules rather than gaps and shadows. */
export function Section({
  title,
  aside,
  children,
}: {
  title: string
  aside?: ReactNode
  children: ReactNode
}) {
  return (
    <section style={{ padding: 'var(--s1)' }}>
      <div
        style={{
          display: 'flex',
          alignItems: 'baseline',
          justifyContent: 'space-between',
          gap: 8,
        }}
      >
        <h2 className="label">{title}</h2>
        {aside}
      </div>
      <hr style={{ margin: '6px 0 var(--s1)' }} />
      {children}
    </section>
  )
}

/** A disclosure whose trigger is a plain line of text, not a button chrome.
 *  Used for the plan's reason, which is the most important expansion here. */
export function Disclosure({
  summary,
  open,
  onToggle,
  children,
}: {
  summary: string
  open: boolean
  onToggle: () => void
  children: ReactNode
}) {
  return (
    <div>
      <button
        onClick={onToggle}
        aria-expanded={open}
        style={{
          border: 0,
          padding: '2px 0',
          width: '100%',
          textAlign: 'left',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: 8,
          color: 'var(--ink-muted)',
        }}
        className="label"
      >
        <span>{summary}</span>
        <span aria-hidden className="mono" style={{ fontSize: 11 }}>
          {open ? '–' : '+'}
        </span>
      </button>
      {open ? <div style={{ paddingTop: 8 }}>{children}</div> : null}
    </div>
  )
}
