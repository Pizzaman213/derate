import type { Cluster, MetricsNodeFrame } from '../../api/types'
import { Readout } from '../../components/Readout'
import { gbNum } from '../../format'
import { runners } from '../cluster/layout'
import type { SafeMetricsFrame } from '../../state/useMetrics'

interface Props {
  cluster: Cluster | null
  frame: SafeMetricsFrame | null
}

/** Four tiles, mockups-next's `#s-overview` quad. The mockup's fourth tile
 *  ("slots free") is dead: there is no slots model, so it is replaced with
 *  addressable memory actually free across the cluster. */
export function AggregateRow({ cluster, frame }: Props) {
  const deployments = cluster?.deployments ?? null
  const serving = deployments
    ? deployments.filter((d) => d.state === 'ready' || d.state === 'degraded').length
    : null

  return (
    <div className="quad" style={{ margin: '4px 0 6px' }}>
      <Tile value={totalTps(deployments, frame)} decimals={0} width={6} label="tokens per second, all models" />
      <Tile value={serving} decimals={0} width={3} label="deployments serving" />
      <Tile value={totalWatts(frame?.nodes ?? null)} decimals={0} width={5} label="watts drawn" />
      <Tile value={cluster ? gbNum(freeBytes(cluster)) : null} decimals={1} width={6} label="GiB addressable free" />
    </div>
  )
}

function Tile({
  value,
  decimals,
  width,
  label,
}: {
  value: number | null
  decimals: number
  width: number
  label: string
}) {
  return (
    <div>
      <Readout value={value} decimals={decimals} width={width} size="readout" align="left" />
      <div className="unit" style={{ marginTop: 4 }}>
        {label}
      </div>
    </div>
  )
}

/** Sum of each running deployment's latest reported rate. `null` (not 0) when
 *  something is up but the stream has not said anything about any of it yet
 *  -- an honest "no reading" beats a confident zero the frame never sent.
 *  Nothing running is a real, known zero and renders as one.
 *
 *  Counted over `runners()`, not over the raw list: `/api/deployments` keeps
 *  every attempt, and a cluster whose rows are all stopped or failed is not
 *  "no reading yet", it is nought tokens per second. Before the filter those
 *  dead rows held the tile at an em dash for as long as the ledger kept
 *  them. */
function totalTps(
  deployments: Cluster['deployments'] | null,
  frame: SafeMetricsFrame | null,
): number | null {
  if (!deployments) return null
  const live = runners(deployments)
  if (live.length === 0) return 0
  if (!frame) return null
  let sum = 0
  let any = false
  for (const d of live) {
    const v = frame.deployments.find((x) => x.deployment_id === d.deployment_id)?.tokens_per_sec
    if (v != null) {
      sum += v
      any = true
    }
  }
  return any ? sum : null
}

/** Same honesty rule as above: a node that has never reported power is
 *  skipped, not treated as drawing zero watts. Only when every node is
 *  missing does the tile itself go to em dash. */
function totalWatts(nodes: MetricsNodeFrame[] | null): number | null {
  if (!nodes) return null
  const values: number[] = []
  for (const n of nodes) {
    if (n.power_w != null) values.push(n.power_w)
  }
  return values.length ? values.reduce((a, b) => a + b, 0) : null
}

function freeBytes(cluster: Cluster): number {
  let addressable = 0
  let used = 0
  for (const n of cluster.nodes) {
    addressable += n.profile.addressable_memory
    used += n.memory_used
  }
  return addressable - used
}
