import type { NodeStateDTO } from '../../api/types'
import type { SelectionApi } from '../../state/selection'
import type { TelemetrySeries } from '../../state/useTelemetry'
import { Chart } from './Chart'

interface Props {
  nodes: NodeStateDTO[]
  telemetry: TelemetrySeries
  selection: SelectionApi
}

// The mockup's own line here ("the control plane stores no history") is no
// longer true as of the telemetry package -- durable journals exist on the
// coordinator now. This is the corrected sentence, not a paraphrase of the
// old one.
const NOTE =
  'Every series below comes from the 1 Hz metrics frame and is accumulated in this browser; this window is 60 seconds and starts empty on load. Durable history exists on the coordinator but is not charted here — there are no percentiles anywhere in the system; TTFT and mean duration are moving averages.'

/** Drill-down, not a wall: cluster charts are always on, per-Spark and
 *  per-deployment charts render only for the current selection. Selection is
 *  shared with the deployments strip and the cluster graph, so drilling in
 *  one place drills everywhere. Nine series total, because that is exactly
 *  what the 1 Hz frame carries -- see state/useTelemetry.ts. */
export function TelemetrySub({ nodes, telemetry, selection }: Props) {
  const selectedNode = nodes.find((n) => n.profile.node_id === selection.selNode)

  return (
    <div style={{ display: 'grid', gap: 'var(--s-4)' }}>
      <p className="unit" style={{ margin: '0 0 4px' }}>
        {NOTE}
      </p>

      <section>
        <h2>Cluster</h2>
        <div className="chartgrid">
          <Chart title="Throughput, all models" unit="tok/s" points={telemetry.clusterTps} />
          <Chart title="Power drawn" unit="W" points={telemetry.clusterPower} />
        </div>
      </section>

      <section>
        <h2>Per Spark</h2>
        <div className="chips">
          {nodes.map((n) => (
            <button
              key={n.profile.node_id}
              aria-pressed={n.profile.node_id === selection.selNode}
              // Picking a Spark here selects it everywhere; it never toggles off.
              onClick={() => selection.pickNode(n.profile.node_id)}
            >
              {n.profile.hostname}
            </button>
          ))}
        </div>
        {selectedNode ? (
          <div className="chartgrid">
            <Chart
              title={`${selectedNode.profile.hostname} · power`}
              unit="W"
              points={telemetry.nodePower[selectedNode.profile.node_id] ?? []}
            />
            <Chart
              title={`${selectedNode.profile.hostname} · temperature`}
              unit="°C"
              points={telemetry.nodeTemp[selectedNode.profile.node_id] ?? []}
            />
            <Chart
              title={`${selectedNode.profile.hostname} · memory`}
              unit="%"
              points={telemetry.nodeMem[selectedNode.profile.node_id] ?? []}
            />
            <Chart
              title={`${selectedNode.profile.hostname} · GPU utilisation`}
              unit="%"
              points={telemetry.nodeUtil[selectedNode.profile.node_id] ?? []}
            />
          </div>
        ) : (
          <p className="unit" style={{ margin: 0 }}>
            Select a Spark above to see its charts.
          </p>
        )}
      </section>

      <section>
        <h2>Per deployment</h2>
        <p className="unit" style={{ margin: '-4px 0 10px' }}>
          {selection.selDep
            ? `Showing ${selection.selDep}. Selecting a model anywhere — the deployments strip or the cluster graph — changes this.`
            : 'Nothing is being served.'}
        </p>
        {selection.selDep ? (
          <div className="chartgrid">
            <Chart
              title={`${selection.selDep} · aggregate throughput`}
              unit="tok/s"
              points={telemetry.depTps[selection.selDep] ?? []}
            />
            <Chart
              title={`${selection.selDep} · time to first token`}
              unit="ms"
              points={telemetry.depTtft[selection.selDep] ?? []}
            />
            <Chart
              title={`${selection.selDep} · queued`}
              unit="reqs"
              points={telemetry.depQueue[selection.selDep] ?? []}
            />
          </div>
        ) : null}
      </section>
    </div>
  )
}
