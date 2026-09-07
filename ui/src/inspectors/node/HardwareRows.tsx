import type { MemoryReport, NodeStateDTO } from '../../api/types'
import { useMemoryReport } from '../../state/resources'
import { fmtUnit, gbytes, relativeTime, shortGpu } from '../../format'

/** What the machine is, and what of it is actually available.
 *
 *  The memory half of this used to be computed here: `addressable - used`,
 *  guarded to unified memory only, which meant every discrete node showed an
 *  em dash for the one number that decides whether anything can be launched on
 *  it. `/api/memory` has carried a real `allocatable` per node all along, from
 *  the fit gate itself, and it is already polled every two seconds for the
 *  Serve button. Reading it here rather than recomputing is the same rule the
 *  quant table follows: a second copy of a figure is a second answer, and the
 *  one that disagrees with the fit gate is the one that gets somebody an OOM. */
export function HardwareRows({ node }: { node: NodeStateDTO }) {
  const p = node.profile
  const report = useMemoryReport()
  const mem: MemoryReport | undefined = report.data?.nodes.find(
    (n) => n.node_id === p.node_id,
  )

  const guardrail = mem?.guardrail ?? 0.9
  const usable = Math.trunc(p.addressable_memory * guardrail)
  // A machine with no GPU has no addressable memory to be a fraction of. Its
  // sample carries the host total instead, which is the only pool it has --
  // and dividing by the absent one left this row reading "0.0 of 0.0 GiB".
  const noGpu = p.gpu_count === 0
  const inUseTotal = p.addressable_memory || node.memory_total

  return (
    <>
      <div className="row">
        <span>GPU</span>
        <span className="mono">
          {shortGpu(p.gpu_name)}
          {p.gpu_count > 1 ? ` × ${p.gpu_count}` : ''}
        </span>
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
          {gbytes(p.addressable_memory, 1)} GiB · {gbytes(usable, 1)} GiB usable at the{' '}
          {guardrail.toFixed(2)} guardrail
        </span>
      </div>
      <div className="row">
        <span>allocatable now</span>
        <span className="mono">
          {mem?.allocatable != null ? `${gbytes(mem.allocatable, 1)} GiB` : '—'}
          {mem?.binding_limit ? (
            <span className="unit"> · {mem.binding_limit} bound</span>
          ) : null}
        </span>
      </div>
      <div className="row">
        <span>{noGpu ? 'host memory in use' : 'in use'}</span>
        <span className="mono">
          {inUseTotal > 0
            ? `${gbytes(node.memory_used, 1)} of ${gbytes(inUseTotal, 1)} GiB`
            : '—'}
          {mem?.memory_severity && mem.memory_severity !== 'ok' ? (
            <span className="unit" style={{ color: 'var(--warn)' }}>
              {' '}
              · {mem.memory_severity}
            </span>
          ) : null}
        </span>
      </div>
      <div className="row">
        <span>host memory free</span>
        <span className="mono">
          {mem?.host_available != null ? `${gbytes(mem.host_available, 1)} GiB` : '—'}
          {mem?.swap_used != null && mem.swap_used > 0 ? (
            <span className="unit"> · {gbytes(mem.swap_used, 1)} GiB swapped</span>
          ) : null}
        </span>
      </div>
      <div className="row">
        <span>memory bandwidth</span>
        <span className="mono">{fmtUnit(p.memory_bandwidth_gbps, 1, 'GB/s')}</span>
      </div>
      <div className="row">
        <span>last heard from</span>
        <span className="mono">
          {node.last_seen ? relativeTime(node.last_seen) : '—'}
          {/* The health check and the telemetry sample are different clocks,
              and a node whose agent answers forever while nvidia-smi is gone
              is the exact case that made SAMPLE_STALE_S necessary. Both, so
              the difference between them is visible. */}
          {node.sample_ts ? (
            <span className="unit"> · sampled {relativeTime(node.sample_ts)}</span>
          ) : null}
        </span>
      </div>
    </>
  )
}
