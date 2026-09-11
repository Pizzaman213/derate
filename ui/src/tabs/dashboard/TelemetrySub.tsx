import type { NodeStateDTO } from '../../api/types'
import type { SelectionApi } from '../../state/selection'
import type { TelemetrySeries } from '../../state/useTelemetry'
import { fromState, nodeName } from '../../state/names'
import { utilLabel } from '../../state/live'
import { Chart, ChartGrid } from './Chart'

interface Props {
  nodes: NodeStateDTO[]
  telemetry: TelemetrySeries
  selection: SelectionApi
}

// The mockup's own line here ("the control plane stores no history") is no
// longer true as of the telemetry package -- durable journals exist on the
// coordinator now. This is the corrected sentence, not a paraphrase of the
// old one. Corrected a second time when the node sheet started reading the
// archive: percentiles DO exist, they were simply unreachable from the
// browser, and saying otherwise on a screen next to one that shows them is
// worse than saying nothing.
const NOTE =
  'Every series below comes from the 1 Hz metrics frame and is accumulated in this browser; this window is 60 seconds and starts empty on load. The coordinator also keeps a durable archive, which a node\u2019s own page charts over longer windows and which carries real TTFT and duration percentiles. Nothing on this screen does: the frame\u2019s TTFT and mean duration are moving averages. The prefix-cache trace is the one series here the coordinator samples on its own slower clock rather than reading off the frame\u2019s own tick, so it steps every ten seconds and is blank wherever nothing was asked of a cache.'

/** Drill-down, not a wall: cluster charts are always on, per-Spark and
 *  per-deployment charts render only for the current selection. Selection is
 *  shared with the deployments strip and the cluster graph, so drilling in
 *  one place drills everywhere. Ten series total, because that is exactly
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
        <ChartGrid>
          <Chart title="Throughput, all models" unit="tok/s" points={telemetry.clusterTps} />
          <Chart title="Power drawn" unit="W" points={telemetry.clusterPower} />
          {/* Gaps here are the ordinary case, not a dropped frame: the
              coordinator scrapes this every 10s against ready vLLMs only, and
              answers null for a window in which nothing was asked of any
              cache. `push` keeps a null as a real point so the trace breaks
              rather than interpolating across it. */}
          <Chart title="Prefix cache hits" unit="%" points={telemetry.clusterCacheHit} />
        </ChartGrid>
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
              {nodeName(fromState(n))}
            </button>
          ))}
        </div>
        {selectedNode ? (
          <ChartGrid>
            <Chart
              title={`${nodeName(fromState(selectedNode))} · power`}
              unit="W"
              points={telemetry.nodePower[selectedNode.profile.node_id] ?? []}
            />
            <Chart
              title={`${nodeName(fromState(selectedNode))} · temperature`}
              unit="°C"
              points={telemetry.nodeTemp[selectedNode.profile.node_id] ?? []}
            />
            <Chart
              title={`${nodeName(fromState(selectedNode))} · memory`}
              unit="%"
              points={telemetry.nodeMem[selectedNode.profile.node_id] ?? []}
            />
            <Chart
              title={`${nodeName(fromState(selectedNode))} · ${utilLabel(selectedNode.profile)}`}
              unit="%"
              points={telemetry.nodeUtil[selectedNode.profile.node_id] ?? []}
            />
          </ChartGrid>
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
          <ChartGrid>
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
          </ChartGrid>
        ) : null}
      </section>
    </div>
  )
}
