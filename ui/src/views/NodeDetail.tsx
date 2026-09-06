import type { Cluster, Topology } from '../api/types'
import type { SafeMetricsFrame } from '../state/useMetrics'
import { Lamp } from '../components/Lamp'
import { Readout } from '../components/Readout'
import { ProportionBar } from '../components/Bars'
import { Verbatim } from '../components/Verbatim'
import { gbytes, pct, relativeTime, shortGpu } from '../format'
import { nodeLive, nodeSignal } from '../panels/NodeRoster'

interface Props {
  nodeId: string
  cluster: Cluster
  topology: Topology
  frame: SafeMetricsFrame | null
  stale: boolean
  onBack: () => void
  onMeasure: (a: string, b: string) => void
  measuring: string | null
}

/** One machine: what it is, what it is doing, and what it is connected to.
 *  Roster data expanded, not a metrics page. */
export function NodeDetail({
  nodeId,
  cluster,
  topology,
  frame,
  stale,
  onBack,
  onMeasure,
  measuring,
}: Props) {
  const node = cluster.nodes.find((n) => n.profile.node_id === nodeId)

  if (!node) {
    return (
      <div style={{ display: 'grid', gap: 'var(--s2)', maxWidth: 620 }}>
        <button onClick={onBack} style={{ justifySelf: 'start' }}>
          Back
        </button>
        <p className="label" style={{ fontWeight: 400, margin: 0 }}>
          {nodeId} is no longer in the cluster.
        </p>
      </div>
    )
  }

  const p = node.profile
  const live = nodeLive(node, frame, stale)
  const signal = nodeSignal(node, live)
  const down = signal === 'fault'
  const grey = down || !live.fresh
  const usable = Math.trunc(p.addressable_memory * 0.9)

  const here = cluster.deployments.filter((d) => d.node_ids.includes(nodeId))
  const edges = topology.edges.filter((e) => e.src === nodeId || e.dst === nodeId)

  return (
    <div style={{ display: 'grid', gap: 'var(--s3)', maxWidth: 780 }}>
      <header style={{ display: 'grid', gap: 'var(--s1)' }}>
        <button onClick={onBack} style={{ justifySelf: 'start' }}>
          Back
        </button>
        <div
          style={{
            display: 'flex',
            alignItems: 'baseline',
            justifyContent: 'space-between',
            gap: 'var(--s2)',
          }}
        >
          <h1 style={{ fontSize: 20, fontWeight: 500 }}>{p.hostname}</h1>
          <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <Lamp signal={signal} hollow={grey && !down} label={node.state} />
            <span className="label" style={{ fontWeight: 400 }}>
              {node.state}
              {node.role === 'coordinator' ? ' · coordinator' : ''}
            </span>
          </span>
        </div>
        {down && node.last_error ? (
          <Verbatim text={node.last_error} size="label" />
        ) : null}
      </header>

      <section style={{ display: 'grid', gap: 'var(--s1)' }}>
        <div
          style={{
            display: 'flex',
            gap: 'var(--s3)',
            flexWrap: 'wrap',
            alignItems: 'baseline',
          }}
        >
          <Big label="memory used" value={live.memory_used_pct} unit="%" width={3} stale={grey} />
          <Big label="utilisation" value={live.util_pct} unit="%" width={3} stale={grey} />
          <Big label="power" value={live.power_w} unit="W" width={3} stale={grey} />
          <Big label="temperature" value={live.temp_c} unit="°C" width={3} stale={grey} />
        </div>
        <ProportionBar
          value={(live.memory_used_pct ?? 0) / 100}
          height={6}
          tone={grey ? 'muted' : 'ink'}
          label={`${pct(live.memory_used_pct)} percent of addressable memory in use`}
        />
        {!live.fresh ? (
          <p className="unit" style={{ margin: 0 }}>
            Last known reading, {relativeTime(node.last_seen)}. Not current.
          </p>
        ) : null}
      </section>

      <hr />

      <section>
        <dl
          style={{
            margin: 0,
            display: 'grid',
            gridTemplateColumns: 'max-content max-content',
            columnGap: 'var(--s2)',
            rowGap: 4,
          }}
        >
          <Row label="device" value={shortGpu(p.gpu_name)} />
          <Row label="device class" value={p.device_class} />
          <Row label="address" value={p.address} mono />
          <Row label="total memory" value={`${gbytes(p.total_memory, 0)} GB`} mono />
          <Row label="addressable" value={`${gbytes(p.addressable_memory, 1)} GB`} mono />
          <Row label="usable at 0.90" value={`${gbytes(usable, 1)} GB`} mono />
          <Row label="memory bandwidth" value={`${p.memory_bandwidth_gbps.toFixed(1)} GB/s`} mono />
          <Row label="compute capability" value={p.compute_capability} mono />
          <Row label="driver" value={p.driver_version} mono />
        </dl>
      </section>

      {node.eligible === false && node.ineligible_reason ? (
        <section style={{ display: 'grid', gap: 6 }}>
          <h2 className="label muted" style={{ fontWeight: 400 }}>
            serving pool
          </h2>
          <Verbatim text={node.ineligible_reason} size="label" />
        </section>
      ) : null}

      <hr />

      <section style={{ display: 'grid', gap: 'var(--s1)' }}>
        <h2 className="label muted" style={{ fontWeight: 400 }}>
          running here
        </h2>
        {here.length === 0 ? (
          <p className="label muted" style={{ fontWeight: 400, margin: 0 }}>
            Nothing.
          </p>
        ) : (
          here.map((d) => (
            <div key={d.deployment_id} style={{ display: 'grid', gap: 2 }}>
              <div style={{ fontWeight: 500 }}>{d.served_name}</div>
              <div className="unit">
                {d.runtime} · {d.state} · {d.context_length} context ·{' '}
                {d.max_concurrent_seqs} concurrent
              </div>
            </div>
          ))
        )}
      </section>

      <hr />

      <section style={{ display: 'grid', gap: 'var(--s1)' }}>
        <h2 className="label muted" style={{ fontWeight: 400 }}>
          links
        </h2>
        {edges.length === 0 ? (
          <p className="label muted" style={{ fontWeight: 400, margin: 0 }}>
            No other machine to link to yet.
          </p>
        ) : (
          edges.map((e) => {
            const other = e.src === nodeId ? e.dst : e.src
            const key = [e.src, e.dst].sort().join('~')
            return (
              <div
                key={key}
                style={{
                  display: 'flex',
                  alignItems: 'baseline',
                  justifyContent: 'space-between',
                  gap: 'var(--s1)',
                }}
              >
                <span className="label" style={{ fontWeight: 400 }}>
                  {other} <span className="unit">{e.medium}</span>
                </span>
                {e.measured && e.all_reduce_gbps != null ? (
                  <Readout value={e.all_reduce_gbps} decimals={1} width={5} unit="GB/s" />
                ) : (
                  <button
                    onClick={() => onMeasure(e.src, e.dst)}
                    disabled={measuring === key}
                    className="label"
                  >
                    {measuring === key ? 'Measuring…' : 'Measure'}
                  </button>
                )}
              </div>
            )
          })
        )}
      </section>
    </div>
  )
}

function Big({
  label,
  value,
  unit,
  width,
  stale,
}: {
  label: string
  value: number | null
  unit: string
  width: number
  stale: boolean
}) {
  return (
    <div style={{ display: 'grid', gap: 4 }}>
      <Readout
        value={value}
        decimals={0}
        width={width}
        unit={unit}
        size="readout"
        align="left"
        stale={stale}
      />
      <span className="unit">{label}</span>
    </div>
  )
}

function Row({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <>
      <dt className="label muted" style={{ fontWeight: 400 }}>
        {label}
      </dt>
      <dd className={mono ? 'mono' : undefined} style={{ margin: 0, fontSize: mono ? 14 : undefined }}>
        {value}
      </dd>
    </>
  )
}
