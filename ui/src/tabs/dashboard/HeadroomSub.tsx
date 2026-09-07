import { useCapacity, useMemoryReport } from '../../state/resources'
import type { CapacityRow, MemoryReport } from '../../api/types'
import { Lamp } from '../../components/Lamp'
import { Readout } from '../../components/Readout'
import { Verbatim } from '../../components/Verbatim'
import { gbNum } from '../../format'

/** What can actually run here, right now.
 *
 *  Every number on this screen is one the fit gate or the registry produced.
 *  Nothing is recomputed in the browser -- that rule is why the planner and
 *  this view cannot disagree, and it is the rule the old client-side
 *  "Allocatable" row broke by subtracting two numbers itself.
 *
 *  Sits second among the sub-tabs, directly under the planner, because it
 *  answers the planner's question in reverse: not "does this model fit" but
 *  "what fits". Telemetry and Load answer what is already running. */
export function HeadroomSub({ context, concurrency }: { context: number; concurrency: number }) {
  const memory = useMemoryReport()
  const capacity = useCapacity(context, concurrency)

  const nodes = memory.data?.nodes ?? []
  const cap = capacity.data

  return (
    <div style={{ display: 'grid', gap: 'var(--s-4)' }}>
      <p className="unit" style={{ margin: 0 }}>
        The static ceiling is what this hardware could spend with nothing else running.
        Allocatable is what it can hand out at this moment, with the operating system and
        every other process already counted. A launch can only rely on the second.
      </p>

      <section>
        <div className="sub">per node</div>
        {memory.error ? (
          <p className="label" style={{ fontWeight: 400, color: 'var(--fault)' }}>
            {memory.error.message}
          </p>
        ) : nodes.length === 0 ? (
          <p className="unit">{memory.loading ? 'Reading memory…' : 'No node reported memory.'}</p>
        ) : (
          <div style={{ display: 'grid', gap: 'var(--s-3)' }}>
            {nodes.map((n) => (
              <NodeCard key={n.node_id} report={n} />
            ))}
          </div>
        )}
      </section>

      <section>
        <div className="sub">what fits right now</div>
        {capacity.error ? (
          <p className="label" style={{ fontWeight: 400, color: 'var(--fault)' }}>
            {capacity.error.message}
          </p>
        ) : !cap ? (
          <p className="unit">{capacity.loading ? 'Asking the fit gate…' : 'No answer.'}</p>
        ) : (
          <>
            <p className="unit" style={{ margin: '0 0 8px' }}>
              At {cap.context.toLocaleString()} context and {cap.concurrency}{' '}
              {cap.concurrency === 1 ? 'sequence' : 'sequences'}, probed on {cap.probed_node}.
            </p>
            <Best label="right now" side={cap.live} fallback={cap.unavailable_reason} />
            <Best label="on idle hardware" side={cap.static ?? null} fallback={null} muted />
            <CapacityTable rows={cap.live?.rows ?? cap.static?.rows ?? []} />
            {cap.unresolved.length > 0 ? (
              <div style={{ marginTop: 10, display: 'grid', gap: 6 }}>
                <div className="unit">not resolved, so not answered for</div>
                {cap.unresolved.map((u) => (
                  <Verbatim key={u.model_id} text={`${u.model_id} — ${u.reason}`} size="label" />
                ))}
              </div>
            ) : null}
            {cap.excluded.length > 0 ? (
              <div style={{ marginTop: 10, display: 'grid', gap: 6 }}>
                <div className="unit">excluded from the budget</div>
                {cap.excluded.map((e) => (
                  <Verbatim key={e.node_id} text={`${e.node_id} — ${e.reason}`} size="label" />
                ))}
              </div>
            ) : null}
          </>
        )}
      </section>
    </div>
  )
}

function Best({
  label,
  side,
  fallback,
  muted,
}: {
  label: string
  side: { best: CapacityRow | null; allocatable_per_node?: number; usable_per_node?: number } | null
  fallback: string | null
  muted?: boolean
}) {
  if (!side) {
    return (
      <p className="unit" style={{ margin: '0 0 6px' }}>
        largest {label}: {fallback ? `unavailable — ${fallback}` : 'unavailable'}
      </p>
    )
  }
  const budget = side.allocatable_per_node ?? side.usable_per_node ?? null
  const b = side.best
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'baseline',
        gap: 10,
        margin: '0 0 6px',
        color: muted ? 'var(--ink-muted)' : 'var(--ink)',
      }}
    >
      <span className="unit">largest {label}</span>
      <span className="label mono" style={{ fontWeight: muted ? 400 : 500 }}>
        {b ? `${b.label}${b.dtype ? ` · ${b.dtype}` : ''}` : 'nothing fits'}
      </span>
      {b?.predicted_decode_tps != null ? (
        <Readout value={b.predicted_decode_tps} decimals={0} width={4} unit="tok/s" />
      ) : null}
      {budget != null ? <span className="unit">against {gbNum(budget).toFixed(1)} GB</span> : null}
    </div>
  )
}

function CapacityTable({ rows }: { rows: CapacityRow[] }) {
  if (rows.length === 0) return null
  return (
    <table style={{ width: '100%', marginTop: 8 }}>
      <thead>
        <tr>
          <th style={{ textAlign: 'left' }}>Model</th>
          <th style={{ textAlign: 'left' }}>Quantization</th>
          <th style={{ textAlign: 'right' }}>Needs</th>
          <th style={{ textAlign: 'right' }}>Decode</th>
          <th style={{ textAlign: 'left' }}>Verdict</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          // Dimmed, not reddened: a model that does not fit is excluded, not
          // faulted, and red here would compete with the real fault lamps.
          <tr key={r.model_id} style={{ opacity: r.fits ? 1 : 0.62 }}>
            <td className="mono">{r.label}</td>
            <td className="mono">
              {r.dtype ?? '—'}
              {r.requantized ? <span className="unit"> requantized</span> : null}
            </td>
            <td style={{ textAlign: 'right' }}>
              {r.total != null ? `${gbNum(r.total).toFixed(1)} GB` : '—'}
            </td>
            <td style={{ textAlign: 'right' }}>
              {r.predicted_decode_tps != null ? `${r.predicted_decode_tps.toFixed(0)} tok/s` : '—'}
            </td>
            <td>
              <span title={r.reason}>{r.verdict.replace('_', ' ')}</span>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function NodeCard({ report }: { report: MemoryReport }) {
  const sev = report.memory_severity ?? null
  const signal = sev === 'critical' ? 'fault' : sev === 'warning' ? 'warn' : 'live'
  const g = (v: number | null | undefined) => (v == null ? '—' : `${gbNum(v).toFixed(1)} GB`)

  // A node that reports no addressable memory has no budget to break down,
  // and four rows of 0.0 GB read as a measurement rather than an absence.
  // It is excluded from the capacity probe for the same reason, so say that
  // once instead of drawing a ceiling and a guardrail it does not have.
  const noMemory = !report.addressable

  return (
    <div className="panel" style={{ padding: 'var(--s-3)' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <Lamp signal={noMemory ? 'idle' : signal} hollow label={noMemory ? 'no memory reported' : sev ?? 'no reading'} />
        <span className="label mono">{report.node_id}</span>
        {report.stale ? <span className="unit">last reading is stale</span> : null}
      </div>

      {noMemory ? (
        <p className="unit" style={{ margin: '6px 0 0' }}>
          Reports no addressable memory, so nothing can be placed here and it is
          left out of the budget.
        </p>
      ) : (
        <>

      {/* Three rows, three distinct words for three distinct numbers. The bug
          this replaces was one word -- "Allocatable" -- over two figures. */}
      <div className="row">
        <span>Addressable</span>
        <span className="mono">{g(report.addressable)}</span>
      </div>
      <div className="row">
        <span>Ceiling</span>
        <span className="mono">
          {g(report.static_ceiling)}
          {report.guardrail != null ? (
            <span className="unit">
              {' '}
              at the {report.guardrail.toFixed(2)} guardrail · what the fit gate budgets against
            </span>
          ) : null}
        </span>
      </div>
      <div className="row">
        <span>Allocatable now</span>
        <span className="mono">
          {g(report.allocatable)}
          {report.binding_limit ? (
            <span className="unit">
              {' '}
              bound by {report.binding_limit === 'host' ? 'host memory' : 'the GPU ceiling'}
            </span>
          ) : null}
        </span>
      </div>
      <div className="row">
        <span>Swap in use</span>
        <span className="mono">
          {g(report.swap_used)}
          {report.swap_total ? <span className="unit"> of {g(report.swap_total)}</span> : null}
        </span>
      </div>
        </>
      )}
    </div>
  )
}
