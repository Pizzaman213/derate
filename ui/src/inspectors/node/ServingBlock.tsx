import type { DeploymentDTO, RoutingConfig } from '../../api/types'
import type { SafeMetricsFrame } from '../../state/useMetrics'
import { Lamp } from '../../components/Lamp'
import { Chart } from '../../tabs/dashboard/Chart'
import { useSelection } from '../../state/selection'
import { useTelemetrySeries } from '../../state/telemetry'
import {
  depSeries,
  requestTotals,
  resolutionNote,
  useRequestHistory,
  type HistoryWindow,
} from '../../state/history'
import { fmt, fmtUnit, planShortFromDegrees, relativeTime } from '../../format'

/** One model this machine is serving: what it is, how fast it is answering,
 *  and — on an archived window — the percentiles the live frame cannot give.
 *
 *  Everything about ROUTING stays in the deployment inspector: which targets
 *  exist, their shares, their circuit state, what each costs. This block is
 *  about the model as this machine experiences it, and the served name is a
 *  button into the other surface rather than a duplicate of it. */
export function ServingBlock({
  dep,
  cfg,
  frame,
  window,
}: {
  dep: DeploymentDTO
  cfg: RoutingConfig | null
  frame: SafeMetricsFrame | null
  window: HistoryWindow
}) {
  const selection = useSelection()
  const live = useTelemetrySeries()
  const history = useRequestHistory(dep.served_name, window)

  const depFrame = frame?.deployments.find((d) => d.deployment_id === dep.deployment_id)
  const local = cfg?.targets.find((t) => t.kind === 'local') ?? null
  const counters = local?.counters ?? null

  const degraded = dep.state === 'degraded'
  const serving = dep.state === 'ready' || degraded

  const fromLive = window === 'live'
  const h = history.data
  const tps = fromLive ? live.depTps[dep.served_name] ?? [] : depSeries(h, 'tps')
  const ttft = fromLive ? live.depTtft[dep.served_name] ?? [] : depSeries(h, 'ttft')
  const third = fromLive ? live.depQueue[dep.served_name] ?? [] : depSeries(h, 'failed')
  const note = (points: number) => (fromLive ? undefined : resolutionNote(h, points))

  const totals = requestTotals(h)

  return (
    <div style={{ marginBottom: 16 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: 8 }}>
        <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <Lamp signal={degraded ? 'warn' : serving ? 'live' : 'fault'} label={dep.state} />
          <button
            className="ghost mono"
            onClick={() => selection.openSheet({ kind: 'dep', id: dep.served_name })}
            title="Open the deployment, with its targets and routing"
          >
            {dep.served_name}
          </button>
        </span>
        <span className="unit">{dep.state}</span>
      </div>

      <div className="unit" style={{ margin: '2px 0 8px' }}>
        {planShortFromDegrees(dep.plan)} · {dep.runtime} ·{' '}
        {dep.context_length.toLocaleString()} ctx · {dep.max_concurrent_seqs} seqs · up{' '}
        {dep.started_at != null ? relativeTime(dep.started_at) : '—'}
      </div>

      <div className="quad">
        <Cell value={fmt(depFrame?.tokens_per_sec ?? null, 0)} unit="tok/s all streams" />
        <Cell value={fmt(counters?.decode_tps ?? null, 0)} unit="tok/s per stream" />
        <Cell value={fmt(depFrame?.ttft_ms ?? null, 0)} unit="ms to first token" />
        <Cell value={fmt(depFrame?.queue_depth ?? null, 0)} unit="queued now" />
      </div>

      <div className="chartgrid" style={{ marginTop: 10 }}>
        <Chart title="Throughput" unit="tok/s" points={tps} note={note(tps.length)} />
        <Chart
          title={fromLive ? 'Time to first token' : 'Time to first token, p50'}
          unit="ms"
          points={ttft}
          note={note(ttft.length)}
        />
        <Chart
          title={fromLive ? 'Queued' : 'Failed'}
          unit={fromLive ? 'reqs' : 'reqs'}
          points={third}
          note={note(third.length)}
        />
      </div>

      {totals ? (
        <>
          <div className="row" style={{ marginTop: 8 }}>
            <span>requests</span>
            <span className="mono">
              {fmt(totals.n, 0)} · {fmt(totals.ok, 0)} ok · {fmt(totals.failed, 0)} failed ·{' '}
              {fmt(totals.tokens, 0)} tokens
              {totals.cost != null ? ` · $${totals.cost.toFixed(4)}` : ''}
            </span>
          </div>
          <div className="row">
            <span>time to first token</span>
            <span className="mono">
              {fmtUnit(totals.ttft.p50, 0, 'ms')} p50 · {fmtUnit(totals.ttft.p90, 0, 'ms')} p90 ·{' '}
              {fmtUnit(totals.ttft.p99, 0, 'ms')} p99
            </span>
          </div>
          <div className="row">
            <span>whole request</span>
            <span className="mono">
              {fmtUnit(totals.duration.p50, 0, 'ms')} p50 ·{' '}
              {fmtUnit(totals.duration.p90, 0, 'ms')} p90 ·{' '}
              {fmtUnit(totals.duration.p99, 0, 'ms')} p99
            </span>
          </div>
          <div className="unit">
            Real percentiles, merged bucket-wise from the archive&rsquo;s histograms — an hourly
            p99 is that hour&rsquo;s p99 and not a mean of sixty of them. They are taken from the
            busiest bucket in this window rather than summed, because a percentile cannot be
            added. Everything in the four readouts above is a moving average from the live
            frame instead.
          </div>
        </>
      ) : null}
    </div>
  )
}

function Cell({ value, unit }: { value: string; unit: string }) {
  return (
    <div>
      <div className="readout">{value}</div>
      <div className="unit">{unit}</div>
    </div>
  )
}
