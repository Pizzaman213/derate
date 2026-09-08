import { useState, type ReactNode } from 'react'
import type { HistoryEnvelope } from '../../api/types'
import type { Resource } from '../../state/backend'
import { Verbatim } from '../../components/Verbatim'
import { useNodeEvents, useNodeLogs, type HistoryWindow } from '../../state/history'

const LEVELS = ['WARNING', 'INFO'] as const
type Level = (typeof LEVELS)[number]

/** Rows rendered per box. The archive is asked for 200; this is what fits in a
 *  320px pane before scrolling stops being reading and starts being scrubbing.
 *  Disclosed in the footer, never silent -- a capped list that does not say so
 *  reads as a complete one. */
const SHOWN = 40

/** What happened on this machine, in its own words.
 *
 *  Deliberately narrow. One node's recent lines over the window already chosen
 *  for the rest of the page -- not a cluster-wide searchable log surface, which
 *  stays out of scope. There is no query box and no logger control: the two
 *  things that turn a diagnostic strip into a log browser are exactly the two
 *  left out.
 *
 *  Warnings and worse by default. That default used to be the only thing
 *  keeping this section readable, and it was not enough: "everything" could
 *  only ever show forty uvicorn.access lines, because the coordinator polls
 *  each node at 1 Hz and those polls were 99.6% of the log stream. They are no
 *  longer recorded, and `/api/history/logs` excludes them from what is already
 *  stored, so the chip now does what it says. */
export function EventsAndLogs({
  nodeId,
  window,
}: {
  nodeId: string
  window: HistoryWindow
}) {
  const [level, setLevel] = useState<Level>('WARNING')
  const events = useNodeEvents(nodeId, window)
  const logs = useNodeLogs(nodeId, level, window)

  if (window === 'live') {
    return (
      <>
        <div className="sub">what happened here</div>
        <div className="unit">
          Events and log lines are recorded, not streamed to this page. Pick a longer window to
          read them.
        </div>
      </>
    )
  }

  return (
    <>
      <div className="sub">what happened here</div>
      <Box
        title="events"
        resource={events}
        total={events.data?.events.length ?? 0}
        empty="Nothing happened on this machine in this window."
      >
        {(events.data?.events ?? []).slice(0, SHOWN).map((e, i) => (
          <div className="logline" key={`${e.ts}:${e.type}:${i}`}>
            <div className="unit">
              {clock(e.ts)} · {e.source}
              {e.served_name ? ` · ${String(e.served_name)}` : ''}
            </div>
            <div className="mono" style={{ fontSize: 'var(--size-mono-label)' }}>
              {e.type}
            </div>
          </div>
        ))}
      </Box>

      <Box
        title="log"
        resource={logs}
        total={logs.data?.logs.length ?? 0}
        empty={
          level === 'WARNING'
            ? 'Nothing at warning or worse. Switch to everything for the rest.'
            : 'This machine logged nothing in this window.'
        }
        control={
          <span className="chips">
            {LEVELS.map((l) => (
              <button key={l} aria-pressed={l === level} onClick={() => setLevel(l)}>
                {l === 'WARNING' ? 'warnings' : 'everything'}
              </button>
            ))}
          </span>
        }
      >
        {(logs.data?.logs ?? []).slice(0, SHOWN).map((l, i) => (
          <div
            className="logline"
            key={`${l.ts}:${i}`}
            // module:lineno, when the record carried one. In the title rather
            // than the row: it is what you want once you have decided a line
            // matters, and noise on every line until then.
            title={typeof l.where === 'string' ? l.where : undefined}
          >
            <div className="unit">
              {clock(l.ts)} · <span style={{ color: severity(l.level) }}>{l.level}</span> ·{' '}
              {l.logger}
            </div>
            {/* The record as written. A log line paraphrased is not a log line,
                and these are already redacted by the handler that shipped them. */}
            <Verbatim text={l.message} size="unit" />
          </div>
        ))}
      </Box>
    </>
  )
}

/** One bordered, internally scrolling pane. Shared by both halves so they read
 *  as one instrument rather than two lists that happen to sit together. */
function Box({
  title,
  resource,
  total,
  empty,
  control,
  children,
}: {
  title: string
  resource: Resource<HistoryEnvelope>
  total: number
  empty: string
  control?: ReactNode
  children: ReactNode
}) {
  return (
    <div className="logbox">
      <h4>
        <span>{title}</span>
        {control}
      </h4>
      <div className="body">
        {resource.error ? (
          <Verbatim text={resource.error.message} size="unit" />
        ) : resource.data == null ? (
          <div className="unit">Reading…</div>
        ) : total === 0 ? (
          <div className="unit">{empty}</div>
        ) : (
          children
        )}
      </div>
      {total > 0 ? (
        <div className="foot unit">
          {total > SHOWN ? `${SHOWN} of ${total} shown, newest first` : `${total}, newest first`}
          {resource.data?.truncated
            ? '. The window held more than one answer can carry, so its oldest end is missing.'
            : '.'}
        </div>
      ) : null}
    </div>
  )
}

function severity(level: string): string | undefined {
  if (level === 'ERROR' || level === 'CRITICAL') return 'var(--fault)'
  if (level === 'WARNING') return 'var(--warn)'
  return undefined
}

/** Wall clock, not "3m ago": these get read against the request table and each
 *  other, and two different relative clocks do not line up. */
function clock(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString()
}
