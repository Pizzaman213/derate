import type { NodeStateDTO } from '../../api/types'
import { Chart } from '../../tabs/dashboard/Chart'
import { useTelemetrySeries } from '../../state/telemetry'
import {
  nodeSeries,
  resolutionNote,
  useNodeHistory,
  type HistoryWindow,
} from '../../state/history'
import { Provenance } from './Provenance'
import { utilLabel } from '../../state/live'

/** The machine's own four traces, over whichever window is selected.
 *
 *  These four are not new: the Dashboard's Telemetry sub-tab has drawn exactly
 *  power, temperature, memory and utilisation per Spark since it existed. What
 *  is new is that they are on the machine's own page, where SelectionRail says
 *  a machine's readouts belong, and that they can be read from the archive
 *  rather than only from the sixty seconds this tab happens to have seen. */
export function NodeCharts({
  node,
  window,
}: {
  node: NodeStateDTO
  window: HistoryWindow
}) {
  const nodeId = node.profile.node_id
  const live = useTelemetrySeries()
  const history = useNodeHistory(nodeId, window)

  const fromLive = window === 'live'
  const h = history.data
  // The profile's own figure, and only as a fallback: a raw sample carries its
  // own `memory_total`, but a rolled bucket does not, and this is the exact
  // denominator serialize.node_payload divides by -- so the chart and the quad
  // above it cannot disagree about what "percent" means.
  const total = node.profile.total_memory

  const power = fromLive ? live.nodePower[nodeId] ?? [] : nodeSeries(h, 'power', total)
  const temp = fromLive ? live.nodeTemp[nodeId] ?? [] : nodeSeries(h, 'temp', total)
  const util = fromLive ? live.nodeUtil[nodeId] ?? [] : nodeSeries(h, 'util', total)
  const mem = fromLive ? live.nodeMem[nodeId] ?? [] : nodeSeries(h, 'mem', total)

  const note = (points: number) => (fromLive ? undefined : resolutionNote(h, points))

  return (
    <>
      <div className="chartgrid">
        <Chart title="Power drawn" unit="W" points={power} note={note(power.length)} />
        <Chart title="Temperature" unit="°C" points={temp} note={note(temp.length)} />
        <Chart title={utilLabel(node.profile)} unit="%" points={util} note={note(util.length)} />
        <Chart title="Memory used" unit="%" points={mem} note={note(mem.length)} />
      </div>
      <div style={{ marginTop: 8 }}>
        <Provenance window={window} resource={history} />
      </div>
    </>
  )
}
