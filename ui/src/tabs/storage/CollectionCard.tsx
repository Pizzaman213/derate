import { useStorage } from '../../state/resources'
import { Lamp } from '../../components/Lamp'
import { Verbatim } from '../../components/Verbatim'
import { relativeTime } from '../../format'
import type { CollectorCursor } from '../../api/types'

const MiB = 1024 ** 2

function bytesLabel(b: number | null | undefined): string {
  if (b == null) return '—'
  if (b >= 1024 ** 3) return `${(b / 1024 ** 3).toFixed(2)} GB`
  if (b >= MiB) return `${(b / MiB).toFixed(1)} MB`
  return `${(b / 1024).toFixed(0)} KB`
}

/** A node is behind when rows are queued that the coordinator has not taken.
 *  Small numbers are normal — collection runs every few seconds — so this only
 *  reports state once it is worth acting on. */
function signal(c: CollectorCursor): 'live' | 'warn' | 'fault' {
  if (c.dropped > 0 || c.last_error) return 'fault'
  if (c.behind > 5000 || (c.age_s ?? 0) > 60) return 'warn'
  return 'live'
}

/** Whether the durable record is actually durable.
 *
 *  This card exists for one number: `dropped`. A node that is dropping journal
 *  rows still serves, still reports live telemetry, and still draws a full
 *  graph — it is simply no longer keeping the history, and nothing else in the
 *  product would ever say so. */
export function CollectionCard() {
  const storage = useStorage()
  const telemetry = storage.data?.telemetry

  if (storage.loading) {
    return (
      <div className="card2">
        <h3>Collection</h3>
        <div className="unit">Measuring…</div>
      </div>
    )
  }

  if (!telemetry?.enabled) {
    return (
      <div className="card2">
        <h3>Collection</h3>
        <div className="unit" style={{ marginBottom: 6 }}>
          Durable telemetry is off, so nothing is being recorded and the graphs
          are whatever this browser has accumulated since it loaded.
        </div>
        {telemetry?.reason ? <Verbatim text={telemetry.reason} size="label" /> : null}
      </div>
    )
  }

  const cursors = telemetry.archive?.nodes ?? []
  const oldest = telemetry.archive?.oldest_sample_ts ?? null
  const rows = telemetry.archive?.rows

  return (
    <div className="card2">
      <h3>Collection</h3>
      <div className="unit" style={{ marginBottom: 10 }}>
        Every node journals locally and the coordinator drains it. A node that
        is behind has not lost anything yet; a node that has dropped rows has.
      </div>

      <div style={{ overflowX: 'auto' }}>
        <table>
          <thead>
            <tr>
              <th></th>
              <th>Node</th>
              <th style={{ textAlign: 'right' }}>Behind</th>
              <th style={{ textAlign: 'right' }}>Dropped</th>
              <th>Last shipped</th>
            </tr>
          </thead>
          <tbody>
            {cursors.map((c) => (
              <tr key={c.node_id}>
                <td>
                  <Lamp
                    signal={signal(c)}
                    label={
                      c.dropped > 0
                        ? `${c.node_id} has dropped ${c.dropped} rows`
                        : `${c.node_id} is ${c.behind} rows behind`
                    }
                  />
                </td>
                <td className="mono">{c.node_id}</td>
                <td className="num">{c.behind.toLocaleString()}</td>
                <td
                  className="num"
                  style={{ color: c.dropped > 0 ? 'var(--fault)' : undefined }}
                >
                  {c.dropped.toLocaleString()}
                </td>
                <td className="unit">
                  {c.last_ship_ts == null ? '—' : relativeTime(c.last_ship_ts)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {cursors
        .filter((c) => c.last_error)
        .map((c) => (
          <div key={c.node_id} style={{ marginTop: 6 }}>
            <span className="label">{c.node_id}: </span>
            <Verbatim text={c.last_error as string} size="label" />
          </div>
        ))}

      <div className="unit" style={{ marginTop: 10 }}>
        Archive {bytesLabel(telemetry.archive?.bytes)}
        {rows
          ? ` · ${rows.samples.toLocaleString()} samples · ${rows.requests.toLocaleString()} requests · ${rows.events.toLocaleString()} events · ${rows.logs.toLocaleString()} logs`
          : ''}
        {oldest ? ` · oldest sample ${relativeTime(oldest)}` : ''}
        {telemetry.journal
          ? ` · this node's journal ${bytesLabel(telemetry.journal.bytes)}, ${telemetry.journal.queued} queued`
          : ''}
      </div>
    </div>
  )
}
