import type { Cluster, DeploymentDTO, Topology } from '../api/types'
import { Lamp } from '../components/Lamp'
import { Readout } from '../components/Readout'
import { Sparkline } from '../components/Sparkline'
import { HISTORY_SECONDS, type SafeMetricsFrame } from '../state/useMetrics'
import { gbNum, gbytes, shortGpu } from '../format'

interface Props {
  cluster: Cluster
  topology: Topology
  frame: SafeMetricsFrame | null
  history: { t: number; v: number }[]
  stale: boolean
  onPlanModel: () => void
}

/** The instrument. One hero number, and everything else subordinate to it. */
export function MainView({
  cluster,
  topology,
  frame,
  history,
  stale,
  onPlanModel,
}: Props) {
  const hero = heroDeployment(cluster.deployments)

  if (!hero) {
    return <IdleHero cluster={cluster} onPlanModel={onPlanModel} />
  }

  const heroFrame = frame?.deployments.find(
    (d) => d.deployment_id === hero.deployment_id,
  )
  const heroTps =
    heroFrame?.tokens_per_sec ??
    topology.deployments.find((d) => d.deployment_id === hero.deployment_id)
      ?.tokens_per_sec ??
    null

  const degraded = hero.state === 'degraded'
  const serving = hero.state === 'ready' || degraded
  const multiple = cluster.deployments.filter(
    (d) => d.state === 'ready' || d.state === 'degraded',
  ).length

  return (
    <div style={{ display: 'grid', gap: 'var(--s3)', maxWidth: 780 }}>
      <header style={{ display: 'grid', gap: 'var(--s2)' }}>
        <div
          style={{
            display: 'flex',
            alignItems: 'baseline',
            justifyContent: 'space-between',
            gap: 'var(--s2)',
          }}
        >
          <h1 style={{ fontSize: 20, fontWeight: 500 }}>{hero.served_name}</h1>
          <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <Lamp
              signal={degraded ? 'warn' : serving ? 'live' : 'fault'}
              hollow={stale}
              label={
                stale
                  ? 'stream disconnected'
                  : degraded
                    ? 'serving, degraded'
                    : 'serving'
              }
            />
            <span className="label" style={{ fontWeight: 400 }}>
              {degraded ? 'serving, degraded' : serving ? 'serving' : hero.state}
            </span>
          </span>
        </div>

        <div>
          <Readout
            value={heroTps}
            decimals={1}
            width={6}
            size="xl"
            align="left"
            stale={stale}
          />
          <div className="unit" style={{ marginTop: 8 }}>
            tokens per second
          </div>
        </div>

        {hero.last_error ? (
          <p
            className="label"
            style={{
              margin: 0,
              fontWeight: 400,
              color: 'var(--warn)',
              whiteSpace: 'pre-wrap',
            }}
          >
            {hero.last_error}
          </p>
        ) : null}
      </header>

      <hr />

      <section style={{ display: 'grid', gap: 'var(--s1)' }}>
        <h2 className="label muted" style={{ fontWeight: 400 }}>
          throughput, last {HISTORY_SECONDS} s
        </h2>
        <Sparkline
          points={history}
          windowSeconds={HISTORY_SECONDS}
          height={132}
          stale={stale}
          label={`Cluster throughput over the last ${HISTORY_SECONDS} seconds`}
        />
      </section>

      <hr />

      <section>
        <dl
          style={{
            margin: 0,
            display: 'grid',
            gridTemplateColumns: 'max-content max-content',
            columnGap: 'var(--s2)',
            rowGap: 'var(--s-half)',
          }}
        >
          <Stat label="first token" value={heroFrame?.ttft_ms ?? null} unit="ms" width={4} stale={stale} />
          <Stat
            label="cache hits"
            value={frame?.cluster.cache_hit_pct ?? null}
            unit="%"
            width={4}
            stale={stale}
          />
          <Stat
            label="total draw"
            value={frame?.cluster.total_power_w ?? null}
            unit="W"
            width={4}
            stale={stale}
          />
          {multiple > 1 ? (
            <Stat
              label="cluster total"
              value={frame?.cluster.tokens_per_sec ?? null}
              unit="tok/s"
              decimals={1}
              width={6}
              stale={stale}
            />
          ) : null}
        </dl>
      </section>
    </div>
  )
}

function Stat({
  label,
  value,
  unit,
  width,
  decimals = 0,
  stale,
}: {
  label: string
  value: number | null
  unit: string
  width: number
  decimals?: number
  stale: boolean
}) {
  return (
    <>
      <dt className="label muted" style={{ fontWeight: 400, alignSelf: 'baseline' }}>
        {label}
      </dt>
      <dd style={{ margin: 0 }}>
        <Readout value={value} decimals={decimals} width={width} unit={unit} stale={stale} />
      </dd>
    </>
  )
}

/** The deployment the instrument is about. The one spanning the most machines
 *  wins, because that is the interesting one; ties break on throughput and then
 *  on id, so the hero does not swap around between refreshes. */
function heroDeployment(deployments: DeploymentDTO[]): DeploymentDTO | null {
  const live = deployments.filter(
    (d) => d.state === 'ready' || d.state === 'degraded',
  )
  if (live.length === 0) return null
  return [...live].sort(
    (a, b) =>
      b.node_ids.length - a.node_ids.length ||
      (b.fit.predicted_decode_tps ?? 0) - (a.fit.predicted_decode_tps ?? 0) ||
      a.deployment_id.localeCompare(b.deployment_id),
  )[0]!
}

/** One machine and nothing running. The hero area holds the machine's own
 *  profile and a way to start something, rather than a zeroed readout or a
 *  feature list that needs a second machine. */
function IdleHero({
  cluster,
  onPlanModel,
}: {
  cluster: Cluster
  onPlanModel: () => void
}) {
  const node = cluster.nodes[0]
  if (!node) {
    return (
      <div style={{ maxWidth: 620, display: 'grid', gap: 'var(--s2)' }}>
        <h1 style={{ fontSize: 20 }}>No machines yet</h1>
        <p className="label" style={{ fontWeight: 400, margin: 0 }}>
          The coordinator is running but no node agent has reported in.
          Discovery is browsing the local subnet.
        </p>
      </div>
    )
  }

  const p = node.profile
  const usable = Math.trunc(p.addressable_memory * 0.9)

  return (
    <div style={{ display: 'grid', gap: 'var(--s3)', maxWidth: 780 }}>
      <header style={{ display: 'grid', gap: 'var(--s2)' }}>
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
            <Lamp signal="idle" label="idle" />
            <span className="label" style={{ fontWeight: 400 }}>
              idle
            </span>
          </span>
        </div>

        <div>
          <Readout
            value={gbNum(usable)}
            decimals={1}
            width={5}
            size="xl"
            align="left"
          />
          <div className="unit" style={{ marginTop: 8 }}>
            GB usable for a model
          </div>
        </div>
      </header>

      <hr />

      <dl
        style={{
          margin: 0,
          display: 'grid',
          gridTemplateColumns: 'max-content max-content',
          columnGap: 'var(--s2)',
          rowGap: 'var(--s-half)',
        }}
      >
        <ProfileRow label="device" value={shortGpu(p.gpu_name)} />
        <ProfileRow label="total memory" value={`${gbytes(p.total_memory, 0)} GB`} mono />
        <ProfileRow
          label="addressable"
          value={`${gbytes(p.addressable_memory, 1)} GB`}
          mono
        />
        <ProfileRow label="usable at the 0.90 guardrail" value={`${gbytes(usable, 1)} GB`} mono />
        <ProfileRow
          label="memory bandwidth"
          value={`${p.memory_bandwidth_gbps.toFixed(1)} GB/s`}
          mono
        />
        <ProfileRow label="driver" value={p.driver_version} mono />
      </dl>

      <hr />

      <div style={{ display: 'grid', gap: 'var(--s1)', justifyItems: 'start' }}>
        <p className="label" style={{ fontWeight: 400, margin: 0 }}>
          {cluster.nodes.length === 1
            ? 'One machine. Pick a model and the planner will check it against the memory above before anything starts.'
            : 'Nothing is deployed. Pick a model and the planner will derive a plan from the measured links.'}
        </p>
        <button onClick={onPlanModel}>Plan a model</button>
      </div>
    </div>
  )
}

function ProfileRow({
  label,
  value,
  mono,
}: {
  label: string
  value: string
  mono?: boolean
}) {
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
