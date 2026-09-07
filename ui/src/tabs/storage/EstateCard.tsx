import { useState } from 'react'
import { useStorage } from '../../state/resources'
import { useBackend } from '../../state/backend'
import { ProportionBar } from '../../components/Bars'
import { gbytes } from '../../format'
import type { EstateEntry, NodeStorage, RetentionPolicy } from '../../api/types'

const MiB = 1024 ** 2

/** Bytes at the scale this card actually deals in. The estate spans four
 *  orders of magnitude — a 69-byte cluster.json next to a 16 GiB archive — so
 *  a single fixed unit renders most rows as "0.0". */
function bytesLabel(b: number | null): string {
  if (b == null) return '—'
  if (b >= 1024 ** 3) return `${gbytes(b, 2)} GB`
  if (b >= MiB) return `${(b / MiB).toFixed(1)} MB`
  if (b >= 1024) return `${(b / 1024).toFixed(0)} KB`
  return `${b} B`
}

function days(seconds: number): string {
  return `${Math.round(seconds / 86400)} d`
}

/** Rows for one node, largest first. Absent components keep their row so a
 *  reader can tell "nothing written yet" from "this build does not write it". */
function EstateRows({ node, retention }: { node: NodeStorage; retention?: RetentionPolicy }) {
  const cap = (e: EstateEntry): number | null => {
    if (!retention) return null
    if (e.key === 'archive') return retention.archive_max_bytes
    if (e.key === 'journal') return retention.journal_max_bytes
    return null
  }

  const rows = [...node.estate].sort((a, b) => (b.bytes ?? -1) - (a.bytes ?? -1))
  const total = rows.reduce((a, e) => a + (e.bytes ?? 0), 0)

  return (
    <>
      <div
        className="row"
        style={{ borderBottom: '1px solid var(--rule)', paddingBottom: 4 }}
      >
        <span className="mono">{node.node_id}</span>
        <span className="unit">{bytesLabel(total)} total</span>
      </div>
      {rows.map((e) => {
        const ceiling = cap(e)
        return (
          <div className="row" key={e.key}>
            <span style={{ opacity: e.exists ? 1 : 0.45 }}>{e.label}</span>
            <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              {ceiling != null && e.bytes != null ? (
                <>
                  <ProportionBar
                    value={Math.min(1, e.bytes / ceiling)}
                    width={70}
                    height={5}
                    label={`${e.label}: ${bytesLabel(e.bytes)} of a ${bytesLabel(ceiling)} ceiling`}
                  />
                  <span className="unit">of {bytesLabel(ceiling)}</span>
                </>
              ) : null}
              <span className="mono" style={{ fontVariantNumeric: 'tabular-nums' }}>
                {bytesLabel(e.bytes)}
              </span>
            </span>
          </div>
        )
      })}
    </>
  )
}

/** What derate itself is storing, itemised, and the horizons that bound it.
 *
 *  The one mutation on this screen lives here. Clearing the resolver cache is
 *  safe by construction — a miss costs one hub round trip — which is why it is
 *  the only thing offered: nothing else under the data root can be deleted
 *  without losing a record the product needs. */
export function EstateCard() {
  const storage = useStorage()
  const { backend, invalidate } = useBackend()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [freed, setFreed] = useState<number | null>(null)

  const retention = storage.data?.retention
  const nodes = (storage.data?.nodes ?? []).filter((n) => n.available)

  const clearCache = async () => {
    if (
      !window.confirm(
        'Clear every cached model resolution?\n\n' +
          'The next model you open re-reads its shape from the hub, which is ' +
          'slower and needs network. Nothing else is affected.',
      )
    )
      return
    setBusy(true)
    setError(null)
    try {
      const res = await backend.clearResolverCache()
      setFreed(res.bytes_freed)
      invalidate()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="card2">
      <h3>What derate stores</h3>
      <div className="unit" style={{ marginBottom: 10 }}>
        Every file this product writes under its data root, measured. Telemetry
        is the only stream that grows on its own; the rest is a record per node,
        deployment and measurement.
      </div>

      {storage.loading ? (
        <div className="unit">Measuring…</div>
      ) : nodes.length === 0 ? (
        <div className="unit">No node reported a data root.</div>
      ) : (
        nodes.map((n) => (
          <EstateRows key={n.node_id} node={n} retention={retention} />
        ))
      )}

      {retention ? (
        <div style={{ marginTop: 12 }}>
          <div className="label" style={{ marginBottom: 4 }}>
            Retention
          </div>
          <div className="unit">
            Raw samples {days(retention.samples_raw_s)} · requests{' '}
            {days(retention.requests_raw_s)} · logs {days(retention.logs_s)} ·
            events {days(retention.events_s)} · 1-minute rollups{' '}
            {days(retention.rollup_1m_s)} · hourly {days(retention.rollup_1h_s)}.
            A node keeps its own journal {days(retention.journal_retention_s)} or{' '}
            {bytesLabel(retention.journal_max_bytes)}, whichever comes first.
          </div>
        </div>
      ) : null}

      <div style={{ marginTop: 12, display: 'flex', alignItems: 'center', gap: 8 }}>
        <button onClick={clearCache} disabled={busy}>
          {busy ? 'Clearing…' : 'Clear resolver cache'}
        </button>
        {freed != null ? (
          <span className="unit">{bytesLabel(freed)} freed</span>
        ) : null}
      </div>

      {error ? (
        <div
          className="label"
          style={{ color: 'var(--fault)', marginTop: 8, whiteSpace: 'pre-wrap' }}
        >
          {error}
        </div>
      ) : null}
    </div>
  )
}
