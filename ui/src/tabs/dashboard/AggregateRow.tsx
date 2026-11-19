import type { ReactNode } from 'react'
import type { Cluster } from '../../api/types'
import { Readout } from '../../components/Readout'
import { gbNum } from '../../format'
import { runners } from '../models/rows'
import type { SafeMetricsFrame } from '../../state/useMetrics'

interface Props {
  cluster: Cluster | null
  frame: SafeMetricsFrame | null
  /** More tiles on the same line (the Overview's usage tiles). */
  children?: ReactNode
}

/** The Overview's one line of tiles: live throughput and deployments serving,
 *  then whatever the caller adds (the usage tiles). Free memory is on
 *  Telemetry (`freeGiB`). */
export function AggregateRow({ cluster, frame, children }: Props) {
  const deployments = cluster?.deployments ?? null
  const serving = deployments
    ? deployments.filter((d) => d.state === 'ready' || d.state === 'degraded').length
    : null

  return (
    <div
      style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fit, minmax(120px, 1fr))',
        gap: 'var(--space-16)',
        margin: 'var(--space-4) 0 var(--space-6)',
      }}
    >
      <Tile value={totalTps(deployments, frame)} width={5} label="tok/s, all models" />
      <Tile value={serving} width={3} label="serving" />
      {children}
    </div>
  )
}

export function Tile({
  value,
  decimals = 0,
  unit,
  width,
  label,
  title,
}: {
  value: number | null
  decimals?: number
  unit?: string
  width: number
  label: string
  /** Hover detail, e.g. the in/out split under a token total. */
  title?: string
}) {
  return (
    <div title={title}>
      <Readout value={value} decimals={decimals} unit={unit} width={width} size="readout" align="left" />
      <div className="unit" style={{ marginTop: 'var(--space-4)' }}>
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

/** Addressable memory free across the cluster, in GiB; null before the first
 *  cluster read. Shown on Telemetry. */
export function freeGiB(cluster: Cluster | null): number | null {
  if (!cluster) return null
  let addressable = 0
  let used = 0
  for (const n of cluster.nodes) {
    addressable += n.profile.addressable_memory
    used += n.memory_used
  }
  return gbNum(addressable - used)
}
