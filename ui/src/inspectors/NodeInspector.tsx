import { useState } from 'react'
import type { DeploymentDTO, GpuProcessDTO, NodeStateDTO } from '../api/types'
import type { SafeMetricsFrame } from '../state/useMetrics'
import { Lamp } from '../components/Lamp'
import { Readout } from '../components/Readout'
import { Verbatim } from '../components/Verbatim'
import { useBackend } from '../state/backend'
import { nodeLive, nodeSignal } from '../state/live'
import { useNodeProcesses } from '../state/resources'
import { useSelection } from '../state/selection'
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
  // The two memory rows the graph's rail used to carry. The rail no longer
  // shows a machine at all, so the sheet is the only place they can live --
  // and on unified memory they are the whole story: the static ceiling is not
  // what you can allocate, and the difference is the number the fit gate
  // actually plans against.
  const usable = Math.trunc(p.addressable_memory * 0.9)
  const allocatable = unified ? p.addressable_memory - node.memory_used : null
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
            {gbytes(p.addressable_memory, 1)} GiB · {gbytes(usable, 1)} GiB usable at the 0.90 guardrail
          </span>
        </div>
        <div className="row">
          <span>allocatable</span>
          <span className="mono">{allocatable != null ? `${gbytes(allocatable, 1)} GiB` : '—'}</span>
        </div>
        <div className="row">
          <span>in use</span>
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

      <ResidentProcesses nodeId={p.node_id} />

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

/** What is actually holding the GPU, whether or not we launched it.
 *
 *  The gap this closes: `nvidia-smi --query-compute-apps` is the only memory
 *  number GB10 will give you, the fit gate plans against it, and until now
 *  nothing in the product could say what was behind it. A leftover
 *  llama-server holding 72 GiB reads here as 72 GiB of headroom gone with no
 *  deployment to explain it, and the only way to get it back was a shell.
 *
 *  A process we launched is deliberately NOT killable from this list. Killing
 *  a backend the router is still dispatching to would leave the deployment
 *  claiming READY while the process is gone, and the operator who clicked it
 *  would see 502s with nothing connecting them to their own click. Those rows
 *  point at the deployment instead, whose Stop drains first. */
function ResidentProcesses({ nodeId }: { nodeId: string }) {
  const { backend, invalidate } = useBackend()
  const { data, loading } = useNodeProcesses(nodeId)
  const selection = useSelection()
  // Keyed by pid, so one row's kill does not grey out the others.
  const [busy, setBusy] = useState<Record<number, boolean>>({})
  const [error, setError] = useState<string | null>(null)
  const [note, setNote] = useState<string | null>(null)

  const kill = async (proc: GpuProcessDTO) => {
    if (
      !window.confirm(
        `Kill ${proc.name} (pid ${proc.pid})? It is holding ${gbytes(proc.gpu_memory, 1)} GiB. ` +
          `SIGTERM first, then SIGKILL if it has not exited in 10 seconds. ` +
          `Anything it is currently serving stops immediately.`,
      )
    )
      return
    setBusy((b) => ({ ...b, [proc.pid]: true }))
    setError(null)
    setNote(null)
    try {
      const result = await backend.killProcess(nodeId, proc.pid)
      // The server's own sentence: it is the only thing that knows whether
      // SIGTERM was enough and whether the driver actually released.
      setNote(result.detail)
      invalidate()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy((b) => ({ ...b, [proc.pid]: false }))
    }
  }

  const processes = data?.processes ?? []

  return (
    <>
      <div className="sub">resident on this GPU</div>
      {loading && data == null ? (
        <div className="unit">Reading…</div>
      ) : processes.length === 0 ? (
        <div className="unit">{data?.reason ?? 'Nothing is holding GPU memory.'}</div>
      ) : (
        processes.map((proc) => (
          <div key={proc.pid} className="row">
            <span className="mono" title={proc.command ?? undefined}>
              {proc.name}
            </span>
            <span style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
              <span className="unit">
                pid {proc.pid} · {gbytes(proc.gpu_memory, 1)} GiB ·{' '}
                {proc.served_name ?? 'not managed here'}
              </span>
              {proc.killable ? (
                <button onClick={() => void kill(proc)} disabled={busy[proc.pid]}>
                  {busy[proc.pid] ? 'Killing…' : 'Kill'}
                </button>
              ) : (
                <button
                  className="ghost"
                  title={proc.not_killable_reason ?? undefined}
                  onClick={() =>
                    proc.served_name &&
                    selection.openSheet({ kind: 'dep', id: proc.served_name })
                  }
                >
                  ↗
                </button>
              )}
            </span>
          </div>
        ))
      )}
      {note ? (
        <div style={{ marginTop: 8 }}>
          <Verbatim text={note} size="label" />
        </div>
      ) : null}
      {error ? (
        <p className="label" style={{ color: 'var(--fault)', fontWeight: 400, margin: '8px 0 0' }}>
          {error}
        </p>
      ) : null}
    </>
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
