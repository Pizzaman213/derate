import type { NodeStateDTO } from '../../api/types'
import { Chart, ChartGrid } from '../../tabs/dashboard/Chart'
import { useTelemetrySeries } from '../../state/telemetry'
import {
  nodeBand,
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

  // The bucket maxima, where the window has any. On `live` and on a raw
  // window every point is already its own maximum, so `nodeBand` returns null
  // and the four charts draw a bare line exactly as before.
  const bands = fromLive
    ? null
    : {
        power: nodeBand(h, 'power', total),
        temp: nodeBand(h, 'temp', total),
        util: nodeBand(h, 'util', total),
        mem: nodeBand(h, 'mem', total),
      }

  const note = (points: number) => (fromLive ? undefined : resolutionNote(h, points))
  // Only the archive knows where its holes are; the live ring has no memory of
  // having missed anything, which is exactly the difference Provenance spells
  // out underneath and the hatching now draws.
  const env = fromLive ? null : h

  // "avg" and "peak" rather than "avg" and "max": the axis line reads
  // `min 6 avg ... max 61 peak`, where both halves are true of a DIFFERENT
  // series -- the lowest bucket average, and the highest reading inside any
  // bucket. Labelling the upper one "max" made it stutter to "max 61 max".
  const common = { envelope: env, bandLabel: 'peak', lowLabel: 'avg' } as const

  return (
    <>
      {/* A grid, not a div: one crosshair across all four, so a spike in power
          can be read against the temperature, utilisation and memory of the
          same instant instead of four separate guesses at where the eye was. */}
      <ChartGrid>
        <Chart
          title="Power drawn"
          unit="W"
          points={power}
          note={note(power.length)}
          band={bands?.power ?? undefined}
          {...common}
        />
        <Chart
          title="Temperature"
          unit="°C"
          points={temp}
          note={note(temp.length)}
          band={bands?.temp ?? undefined}
          {...common}
        />
        <Chart
          title={utilLabel(node.profile)}
          unit="%"
          points={util}
          note={note(util.length)}
          band={bands?.util ?? undefined}
          {...common}
        />
        <Chart
          title="Memory used"
          unit="%"
          points={mem}
          note={note(mem.length)}
          band={bands?.mem ?? undefined}
          {...common}
        />
      </ChartGrid>
      <div style={{ marginTop: 8 }}>
        <Provenance window={window} resource={history} />
      </div>
    </>
  )
}
