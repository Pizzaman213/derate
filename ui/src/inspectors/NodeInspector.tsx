import type { DeploymentDTO, NodeStateDTO } from '../api/types'
import type { SafeMetricsFrame } from '../state/useMetrics'
import { Lamp } from '../components/Lamp'
import { Readout } from '../components/Readout'
import { Verbatim } from '../components/Verbatim'
import { nodeLive, nodeSignal } from '../state/live'
import { gbytes, shortGpu } from '../format'

interface Props {
  node: NodeStateDTO
  deployments: DeploymentDTO[]
  frame: SafeMetricsFrame | null
  stale: boolean
  onClose: () => void
}

/** The node sheet. Ported from mockups-next/js/inspectors.js's `inspect()`,
 *  minus the two things that do not survive the real wire: a decode row (no
 *  per-node tok/s exists to show) and the throttle slider (there is nothing
 *  on the wire to throttle, and the mockup's own consequence sentence was
 *  entirely about that control). */
export function NodeInspector({ node, deployments, frame, stale, onClose }: Props) {
  const p = node.profile
  const live = nodeLive(node, frame, stale)
  const signal = nodeSignal(node, live)
  const grey = signal === 'fault' || !live.fresh
  const unified = p.device_class === 'gb10'
  const here = deployments.filter((d) => d.node_ids.includes(p.node_id))

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
        <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <Lamp signal={signal} hollow={grey && signal !== 'fault'} label={node.state} />
          <span className="label mono" style={{ fontSize: 16 }}>
            {p.node_id}
          </span>
        </span>
        <button onClick={onClose}>Close</button>
      </div>
      <div className="unit" style={{ margin: '4px 0 14px' }}>
        {shortGpu(p.gpu_name)} · {unified ? 'unified memory' : 'discrete'} · {p.memory_bandwidth_gbps.toFixed(1)} GB/s
      </div>

      <div className="quad">
        <Stat label="power" value={live.power_w} decimals={0} unit="W" stale={grey} />
        <Stat label="temperature" value={live.temp_c} decimals={0} unit="°C" stale={grey} />
        <Stat label="GPU utilisation" value={live.util_pct} decimals={0} unit="%" stale={grey} />
        <Stat label="memory used" value={live.memory_used_pct} decimals={0} unit="%" stale={grey} />
      </div>

      <div style={{ marginTop: 14 }}>
        <div className="row">
          <span>GPU</span>
          <span className="mono">{shortGpu(p.gpu_name)}</span>
        </div>
        <div className="row">
          <span>compute capability</span>
          <span className="mono">{p.compute_capability}</span>
        </div>
        <div className="row">
          <span>driver</span>
          <span className="mono">{p.driver_version}</span>
        </div>
        <div className="row">
          <span>addressable</span>
          <span className="mono">
            {gbytes(node.memory_used, 1)} of {gbytes(p.addressable_memory, 1)} GiB
          </span>
        </div>
      </div>

      <div className="sub">deployments on this node</div>
      {here.length === 0 ? (
        <div className="unit">Nothing.</div>
      ) : (
        here.map((d) => (
          <div key={d.deployment_id} className="row">
            <span>{d.served_name}</span>
            <span className="unit">
              {d.runtime} · {d.state} · {d.context_length.toLocaleString()} ctx · {d.max_concurrent_seqs} seqs
            </span>
          </div>
        ))
      )}

      {node.eligible === false && node.ineligible_reason ? (
        <div style={{ marginTop: 14, paddingTop: 12, borderTop: '1px solid var(--rule)' }}>
          <Verbatim text={node.ineligible_reason} size="label" />
        </div>
      ) : null}

      <div style={{ marginTop: 14, paddingTop: 12, borderTop: '1px solid var(--rule)' }}>
        <div className="unit">
          {unified
            ? 'Unified memory: the model and the operating system share one pool, so the static ceiling overstates what is actually allocatable. Only power, temperature, memory and GPU utilisation refresh live; the rest is read at fetch time.'
            : 'Discrete memory. Only power, temperature, memory and GPU utilisation refresh live.'}
        </div>
      </div>
    </div>
  )
}

function Stat({
  label,
  value,
  decimals,
  unit,
  stale,
}: {
  label: string
  value: number | null
  decimals: number
  unit: string
  stale: boolean
}) {
  return (
    <div>
      <Readout value={value} decimals={decimals} width={3} size="readout" stale={stale} />
      <div className="unit">{unit} {label}</div>
    </div>
  )
}
