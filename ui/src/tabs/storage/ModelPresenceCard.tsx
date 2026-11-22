import type { Resource } from '../../state/backend'
import type { NodeTransfer, ReplicationState, StorageReport } from '../../api/types'
import { VerbatimList } from '../../components/Verbatim'
import { sizeLabel } from '../../format'
import { presenceTable, type PresenceCell, type PresenceState } from './presence'

const TONE: Record<PresenceState, string | undefined> = {
  present: undefined,
  stale: 'var(--warn)',
  partial: 'var(--warn)',
  copying: 'var(--live)',
  proposed: 'var(--warn)',
  wontfit: 'var(--ink-muted)',
  absent: 'var(--ink-muted)',
  unknown: 'var(--ink-muted)',
}

function label(c: PresenceCell): string {
  switch (c.state) {
    case 'present':
      return c.bytes != null ? sizeLabel(c.bytes) : 'present'
    case 'stale':
      return c.bytes != null ? `older revision · ${sizeLabel(c.bytes)}` : 'older revision'
    case 'partial':
      return c.bytes != null ? `partial · ${sizeLabel(c.bytes)}` : 'partial'
    case 'copying':
      return c.fraction != null ? `copying ${Math.floor(c.fraction * 100)}%` : 'copying'
    case 'proposed':
      return 'proposed'
    case 'wontfit':
      return "won't fit"
    case 'absent':
      return '—'
    case 'unknown':
      return 'unknown'
  }
}

/** Every downloaded model against every node, in one compact table: where it
 *  is, where a copy is running or waiting for approval, and where it could
 *  never run. The per-cell decision is `presence.ts` (verified by
 *  `presence.check.mjs`); this only draws it. Sentences behind a won't-fit or
 *  an unreadable node are listed under the table, verbatim. */
export function ModelPresenceCard({
  storage,
  replication,
  transfers,
  coordinatorId,
}: {
  storage: Resource<StorageReport | null>
  replication: Resource<ReplicationState>
  transfers: Resource<{ transfers: NodeTransfer[] }>
  coordinatorId: string | null
}) {
  const table = presenceTable(
    storage.data?.nodes ?? [],
    replication.data,
    transfers.data?.transfers ?? [],
    coordinatorId,
  )
  const notes = [
    ...new Set(
      table.rows.flatMap((r) =>
        Object.values(r.cells)
          .filter((c) => (c.state === 'wontfit' || c.state === 'unknown') && c.note)
          .map((c) => c.note as string),
      ),
    ),
  ]

  return (
    <div className="card2">
      <h3>Models by node</h3>
      <div className="unit" style={{ marginBottom: 'var(--space-10)' }}>
        Each downloaded model against each node: its size where it is on disk,
        a copy running or waiting for your OK, and where it could never be
        served.
      </div>
      {storage.loading ? (
        <div className="unit">Measuring…</div>
      ) : !table.rows.length ? (
        <div className="unit">No node reported a downloaded model.</div>
      ) : (
        <div style={{ overflowX: 'auto' }}>
          <table>
            <thead>
              <tr>
                <th>Model</th>
                {table.nodes.map((n) => (
                  <th key={n} className="mono" style={{ textAlign: 'right' }}>
                    {n}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {table.rows.map((r) => (
                <tr key={r.folder}>
                  <td className="mono" title={r.folder}>
                    {r.repo_id}
                  </td>
                  {table.nodes.map((n) => {
                    const c = r.cells[n]
                    if (!c) return <td key={n} />
                    return (
                      <td
                        key={n}
                        className="num"
                        style={{ color: TONE[c.state] }}
                        title={c.note ?? undefined}
                      >
                        {label(c)}
                      </td>
                    )
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {notes.length ? (
        <div style={{ marginTop: 'var(--space-8)' }}>
          <VerbatimList items={notes} />
        </div>
      ) : null}
    </div>
  )
}
