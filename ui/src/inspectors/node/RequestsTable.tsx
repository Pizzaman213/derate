import type { RequestHistoryRow } from '../../api/types'
import { Verbatim } from '../../components/Verbatim'
import { useRequestHistory, bucketSeconds, type HistoryWindow } from '../../state/history'
import { fmt } from '../../format'

const SHOWN = 50

/** The requests that actually ran on this machine.
 *
 *  Queried WITHOUT a served-name filter and narrowed here by `node_id`, which
 *  is the one shape that answers the question being asked. The route filters by
 *  served name and target, never by node, so filtering server-side would mean
 *  one query per model and a cap on how many models a page can cover; and it
 *  would still be the wrong set, because a request to a model this machine
 *  serves may have been answered by a remote provider under the same name.
 *  `node_id` is on every raw row and it means exactly "ran here".
 *
 *  Individual rows only exist at raw resolution. Past six hours the archive
 *  keeps buckets, not requests, and there is nothing to list -- said in words,
 *  because an empty table reads as "this machine served nothing". */
export function RequestsTable({
  nodeId,
  window,
}: {
  nodeId: string
  window: HistoryWindow
}) {
  const history = useRequestHistory('', window, 500)

  if (window === 'live') {
    return (
      <div className="unit">
        The live window is the 1 Hz frame, which carries rates rather than requests. Pick a
        longer window to read the rows the archive kept.
      </div>
    )
  }
  if (history.error) return <Verbatim text={history.error.message} size="unit" />

  const h = history.data
  if (!h) return <div className="unit">Reading…</div>

  if (bucketSeconds(h.resolution) != null) {
    return (
      <div className="unit">
        This window is kept as buckets rather than individual requests, so there are no rows to
        list. The percentiles beside each model are computed from every request in it.
      </div>
    )
  }

  const rows: RequestHistoryRow[] = h.requests.filter((r) => r.node_id === nodeId)
  rows.sort((x, y) => y.ts - x.ts)
  const elsewhere = h.requests.length - rows.length

  if (rows.length === 0) {
    return (
      <div className="unit">
        No request in this window ran on this machine.
        {elsewhere > 0
          ? ` The window holds ${elsewhere} that ran elsewhere in the cluster.`
          : ' The window holds none at all.'}
      </div>
    )
  }

  return (
    <>
      <div style={{ overflowX: 'auto' }}>
        <table>
          <thead>
            <tr>
              <th>When</th>
              <th>Model</th>
              <th>Status</th>
              <th style={{ textAlign: 'right' }}>Tokens</th>
              <th style={{ textAlign: 'right' }}>TTFT</th>
              <th style={{ textAlign: 'right' }}>Duration</th>
            </tr>
          </thead>
          <tbody>
            {rows.slice(0, SHOWN).map((r) => (
              <tr key={`${r.request_id}:${r.attempt_no}`}>
                <td className="mono">{clock(r.ts)}</td>
                <td className="mono">{r.served_name}</td>
                <td className="mono">
                  <span style={failed(r) ? { color: 'var(--fault)' } : undefined}>
                    {r.error_code || r.status || '—'}
                  </span>
                  {r.attempts != null && r.attempts > 1 ? (
                    <span className="unit"> · {r.attempts} attempts</span>
                  ) : null}
                </td>
                <td className="num">
                  {fmt(r.tokens ?? null, 0)}
                  {r.tokens_estimated ? <span className="unit"> est.</span> : null}
                </td>
                <td className="num">{fmt(r.ttft_ms ?? null, 0)}</td>
                <td className="num">{fmt(r.duration_ms ?? null, 0)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="unit" style={{ marginTop: 6 }}>
        One row per attempt, newest first
        {rows.length > SHOWN ? `; ${SHOWN} of ${rows.length} that ran here are shown` : ''}.
        {elsewhere > 0
          ? ` A further ${elsewhere} request${elsewhere === 1 ? '' : 's'} in this window ran elsewhere in the cluster and ${elsewhere === 1 ? 'is' : 'are'} not listed.`
          : ''}
        {h.truncated
          ? ' The window was wider than one answer can carry, so its oldest end is missing.'
          : ''}{' '}
        A token count marked est. was counted from stream frames rather than read from an
        upstream usage block.
      </div>
    </>
  )
}

function failed(r: RequestHistoryRow): boolean {
  return Boolean(r.error_code) || (r.status != null && r.status >= 400)
}

/** Wall clock rather than "3m ago": these rows get read against the events and
 *  log lines beside them, and two different relative clocks do not line up. */
function clock(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString()
}
