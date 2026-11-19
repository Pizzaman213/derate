import { useState, type CSSProperties, type ReactNode } from 'react'
import type { AuditEntry, AuditPage } from '../../api/types'
import { Icon } from '../../components/Icon'
import { Verbatim } from '../../components/Verbatim'
import { useKeyedResource, type Resource } from '../../state/backend'
import { AuditTable, Pager, TargetCell, rowStyle } from './AuditTable'
import {
  ABSENT,
  AUDIT_PAGE_SIZE,
  actorEventsQuery,
  actorIpsQuery,
  actorKeysQuery,
  diffEntry,
  formatValue,
  ipSummary,
  pageCount,
} from './diff'

const NOWRAP: CSSProperties = { whiteSpace: 'nowrap' }
const SECTION: CSSProperties = { marginTop: 'var(--space-16)' }
const HEADING: CSSProperties = { marginBottom: 'var(--space-6)' }

function When({ ts }: { ts: number }) {
  return <time dateTime={new Date(ts * 1000).toISOString()}>{new Date(ts * 1000).toLocaleString()}</time>
}

/** Loading and error states for one section's read, so each section says
 *  for itself what it is waiting on. */
function Loaded<T>({ res, label, children }: { res: Resource<T>; label: string; children: (d: T) => ReactNode }) {
  if (res.data) return <>{children(res.data)}</>
  if (res.error) return <Verbatim text={res.error.message} size="unit" />
  return (
    <div className="unit" aria-busy="true" aria-live="polite">
      Reading {label}…
    </div>
  )
}

/** The actor sub-tab of the audit log: one admin's event in full, where it
 *  acted from, the keys it created and everything it ever did, all time.
 *  Opened by double-clicking a log row; double-clicking a row here only
 *  changes "This event" -- the actor stays the same.
 *
 *  Keys are what the log recorded (`key_created` rows), not the live key
 *  list: a key deleted since is still listed, which is the point when
 *  re-checking an account after a suspected intrusion. */
export function ActorPanel({
  actor,
  entry,
  onSelect,
}: {
  actor: string
  entry: AuditEntry
  onSelect: (e: AuditEntry) => void
}) {
  const [page, setPage] = useState(1)
  const events = useKeyedResource<AuditPage>(
    `events:${actor}:${page}`,
    (b) => b.audit(actorEventsQuery(actor, page)),
    15000,
  )
  const keys = useKeyedResource<AuditPage>(`keys:${actor}`, (b) => b.audit(actorKeysQuery(actor)), 15000)
  const ips = useKeyedResource<AuditPage>(`ips:${actor}`, (b) => b.audit(actorIpsQuery(actor)), 15000)

  return (
    <div>
      <div>
        <span className="mono">{actor}</span>
        {events.data ? (
          <span className="unit">
            {' · '}
            <span className="mono">{events.data.total}</span> {events.data.total === 1 ? 'event' : 'events'} recorded
          </span>
        ) : null}
      </div>

      <section style={SECTION} aria-label="This event">
        <div className="unit" style={HEADING}>
          This event
        </div>
        <dl
          style={{
            display: 'grid',
            gridTemplateColumns: 'max-content 1fr',
            gap: 'var(--space-4) var(--space-12)',
            alignItems: 'baseline',
            margin: 0,
          }}
        >
          <dt className="unit">When</dt>
          <dd className="mono" style={{ margin: 0 }}>
            <When ts={entry.ts} />
          </dd>
          <dt className="unit">Actor</dt>
          <dd style={{ margin: 0 }}>{entry.actor || ABSENT}</dd>
          <dt className="unit">Action</dt>
          <dd className="mono" style={{ margin: 0 }}>
            {entry.action}
          </dd>
          <dt className="unit">Target</dt>
          <dd style={{ margin: 0 }}>
            <TargetCell kind={entry.target_kind} id={entry.target_id} />
          </dd>
          <dt className="unit">IP</dt>
          <dd className="mono" style={{ margin: 0 }}>
            {entry.ip ?? ABSENT}
          </dd>
          <dt className="unit">Request</dt>
          <dd className="mono" style={{ margin: 0 }}>
            {entry.request_id ?? ABSENT}
            {entry.status != null ? ` · ${entry.status}` : ''}
          </dd>
          <dt className="unit">Session</dt>
          <dd className="mono" style={{ margin: 0 }}>
            {entry.session_id ?? ABSENT}
          </dd>
          <dt className="unit">User agent</dt>
          <dd className="mono" style={{ margin: 0, overflowWrap: 'anywhere' }}>
            {entry.user_agent ?? ABSENT}
          </dd>
          <dt className="unit">Changes</dt>
          <dd style={{ margin: 0 }}>
            <ChangesList before={entry.before} after={entry.after} />
          </dd>
        </dl>
      </section>

      <section style={SECTION} aria-label="Seen from IPs">
        <div className="unit" style={HEADING}>
          Seen from IPs
        </div>
        <Loaded res={ips} label="addresses">
          {(d) => {
            if (d.entries.length === 0) return <div className="unit">No events recorded.</div>
            return (
              <>
                <div style={{ overflowX: 'auto' }}>
                  <table>
                    <thead>
                      <tr>
                        <th scope="col">IP</th>
                        <th scope="col">Events</th>
                        <th scope="col">First seen</th>
                        <th scope="col">Last seen</th>
                      </tr>
                    </thead>
                    <tbody>
                      {ipSummary(d.entries).map((s) => (
                        <tr key={s.ip ?? ''}>
                          <td className="mono" style={NOWRAP}>
                            {s.ip ?? ABSENT}
                          </td>
                          <td className="mono" style={NOWRAP}>
                            {s.count}
                          </td>
                          <td className="mono" style={NOWRAP}>
                            <When ts={s.first} />
                          </td>
                          <td className="mono" style={NOWRAP}>
                            <When ts={s.last} />
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                {d.total > d.entries.length ? (
                  <div className="unit">
                    Based on the newest {d.entries.length} of {d.total} events.
                  </div>
                ) : null}
              </>
            )
          }}
        </Loaded>
      </section>

      <section style={SECTION} aria-label="Keys created">
        <div className="unit" style={HEADING}>
          Keys created
        </div>
        <Loaded res={keys} label="keys">
          {(d) => {
            if (d.total === 0) return <div className="unit">This actor has created no keys.</div>
            return (
              <>
                <div style={{ overflowX: 'auto' }}>
                  <table>
                    <thead>
                      <tr>
                        <th scope="col">When</th>
                        <th scope="col">Key</th>
                        <th scope="col">Name</th>
                        <th scope="col">Org</th>
                        <th scope="col">IP</th>
                      </tr>
                    </thead>
                    <tbody>
                      {d.entries.map((e) => (
                        <tr
                          key={e.id}
                          tabIndex={0}
                          title="Double-click to show this event"
                          style={rowStyle(true, e.id === entry.id)}
                          aria-selected={e.id === entry.id || undefined}
                          onDoubleClick={() => {
                            window.getSelection()?.removeAllRanges()
                            onSelect(e)
                          }}
                          onKeyDown={(ev) => {
                            if (ev.key === 'Enter' && ev.target === ev.currentTarget) onSelect(e)
                          }}
                        >
                          <td className="mono" style={NOWRAP}>
                            <When ts={e.ts} />
                          </td>
                          <td style={NOWRAP}>
                            <TargetCell kind="key" id={e.target_id} />
                          </td>
                          <td style={NOWRAP}>{String(e.after?.name ?? ABSENT)}</td>
                          <td className="mono" style={NOWRAP}>
                            {String(e.after?.org_id ?? ABSENT)}
                          </td>
                          <td className="mono" style={NOWRAP}>
                            {e.ip ?? ABSENT}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                {d.total > d.entries.length ? (
                  <div className="unit">
                    Showing the newest {d.entries.length} of {d.total} keys.
                  </div>
                ) : null}
              </>
            )
          }}
        </Loaded>
      </section>

      <section style={SECTION} aria-label="All activity">
        <div className="unit" style={HEADING}>
          All activity
        </div>
        <Loaded res={events} label="activity">
          {(d) =>
            d.entries.length === 0 ? (
              <div className="unit">No other events.</div>
            ) : (
              <>
                <AuditTable entries={d.entries} showActor={false} selectedId={entry.id} onOpen={onSelect} />
                <Pager
                  page={d.page}
                  pages={pageCount(d.total, d.page_size || AUDIT_PAGE_SIZE)}
                  total={d.total}
                  noun={['event', 'events']}
                  onPage={setPage}
                />
              </>
            )
          }
        </Loaded>
      </section>
    </div>
  )
}

/** Every field an event changed, one per line: the full form of the log
 *  row's one-line summary. */
function ChangesList({
  before,
  after,
}: {
  before: Record<string, unknown> | null
  after: Record<string, unknown> | null
}) {
  const changes = diffEntry(before, after)
  if (changes.length === 0) return <span className="unit">{ABSENT}</span>
  return (
    <ul style={{ listStyle: 'none', margin: 0, padding: 0, display: 'grid', gap: 'var(--space-2)' }}>
      {changes.map((c) => (
        <li key={c.path} style={{ display: 'flex', gap: 'var(--space-6)', alignItems: 'center', flexWrap: 'wrap' }}>
          <span className="mono">{c.path}</span>
          <span className="mono" style={{ color: 'var(--ink-muted)' }}>
            {formatValue(c.before)}
          </span>
          <Icon
            name="arrow-right"
            size={12}
            label={c.kind === 'changed' ? 'changed to' : c.kind === 'added' ? 'added as' : 'removed'}
          />
          <span className="mono">{formatValue(c.after)}</span>
        </li>
      ))}
    </ul>
  )
}
