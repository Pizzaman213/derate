import { useState } from 'react'
import { Verbatim } from '../../components/Verbatim'
import { useNodeEvents, useNodeLogs, type HistoryWindow } from '../../state/history'

const LEVELS = ['WARNING', 'INFO'] as const

/** What happened on this machine, in its own words.
 *
 *  Deliberately narrow. This is one node's recent lines over the window
 *  already chosen for everything else on the page -- not a cluster-wide
 *  searchable log surface, which stays out of scope. There is no query box and
 *  no logger filter: the two controls that turn a diagnostic strip into a log
 *  browser are exactly the two left out.
 *
 *  Warnings and worse by default, because the level filter means "this and
 *  worse" server-side and a healthy node should render a short section rather
 *  than a wall of INFO nobody reads. */
export function EventsAndLogs({
  nodeId,
  window,
}: {
  nodeId: string
  window: HistoryWindow
}) {
  const [level, setLevel] = useState<(typeof LEVELS)[number]>('WARNING')
  const events = useNodeEvents(nodeId, window)
  const logs = useNodeLogs(nodeId, level, window)

  if (window === 'live') {
    return (
      <div className="unit">
        Events and logs are recorded, not streamed to this page. Pick a longer window to read
        them.
      </div>
    )
  }

  return (
    <>
      <div className="sub">events</div>
      {events.error ? (
        <Verbatim text={events.error.message} size="unit" />
      ) : events.data == null ? (
        <div className="unit">Reading…</div>
      ) : events.data.events.length === 0 ? (
        <div className="unit">Nothing happened on this machine in this window.</div>
      ) : (
        events.data.events.slice(0, 40).map((e, i) => (
          <div className="row" key={`${e.ts}:${e.type}:${i}`}>
            <span className="mono">
              {e.type}
              {e.served_name ? <span className="unit"> · {String(e.served_name)}</span> : null}
            </span>
            <span className="unit">
              {e.source} · {clock(e.ts)}
            </span>
          </div>
        ))
      )}

      <div className="sub" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
        <span>log</span>
        <span className="chips">
          {LEVELS.map((l) => (
            <button key={l} aria-pressed={l === level} onClick={() => setLevel(l)}>
              {l === 'WARNING' ? 'warnings' : 'everything'}
            </button>
          ))}
        </span>
      </div>
      {logs.error ? (
        <Verbatim text={logs.error.message} size="unit" />
      ) : logs.data == null ? (
        <div className="unit">Reading…</div>
      ) : logs.data.logs.length === 0 ? (
        <div className="unit">
          {level === 'WARNING'
            ? 'Nothing at warning or worse. Switch to everything to see the rest.'
            : 'This machine logged nothing in this window.'}
        </div>
      ) : (
        logs.data.logs.slice(0, 40).map((l, i) => (
          <div key={`${l.ts}:${i}`} style={{ padding: '4px 0' }}>
            <div className="unit">
              <span style={{ color: severity(l.level) }}>{l.level}</span> · {l.logger} ·{' '}
              {clock(l.ts)}
            </div>
            {/* The record as it was written. A log line paraphrased is not a
                log line, and these are already redacted at the handler that
                shipped them. */}
            <Verbatim text={l.message} size="unit" />
          </div>
        ))
      )}
    </>
  )
}

function severity(level: string): string | undefined {
  if (level === 'ERROR' || level === 'CRITICAL') return 'var(--fault)'
  if (level === 'WARNING') return 'var(--warn)'
  return undefined
}

function clock(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString()
}
