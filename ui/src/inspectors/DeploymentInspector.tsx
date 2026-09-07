import { useState } from 'react'
import { isAudio } from '../api/types'
import type { DeploymentDTO, NodeStateDTO, RouteTarget, RoutingConfig, Settings } from '../api/types'
import { useBackend } from '../state/backend'
import type { SafeMetricsFrame } from '../state/useMetrics'
import { Lamp } from '../components/Lamp'
import { ProportionBar } from '../components/Bars'
import { Verbatim, VerbatimList } from '../components/Verbatim'
import { nodeLive } from '../state/live'
import { fromState, nameIndex } from '../state/names'
import { fmt, fmtUnit, pct, planShortFromDegrees, relativeTime } from '../format'

interface Props {
  dep: DeploymentDTO
  cfg: RoutingConfig | null
  nodes: NodeStateDTO[]
  frame: SafeMetricsFrame | null
  stale: boolean
  settings: Settings | null
  onClose: () => void
}

function targetLabel(t: RouteTarget): string {
  if (t.kind === 'local' && t.node_ids && t.node_ids.length > 0) return t.node_ids.join(' + ')
  return t.target_id
}

/** Local generation has no published price -- it is derived from what was
 *  actually measured: this target's own decode rate and the live power draw
 *  of the node(s) behind it, at the electricity rate a human entered in
 *  Settings. `energy per Mtok (kWh) = power_w * (1e6/tps) s / 3.6e6`, times
 *  the rate -- a physics conversion, not a fitted constant, and it returns
 *  null (never a fabricated number) whenever any input is missing. */
function deriveLocalCost(
  powerW: number | null,
  decodeTps: number | null,
  rate: number,
): number | null {
  if (powerW == null || decodeTps == null || decodeTps <= 0 || rate <= 0) return null
  return (powerW * rate) / (3.6 * decodeTps)
}

/** The deployment sheet. Ported from mockups-next/js/inspectors.js's
 *  `inspectDep()`. No dtype (not on the wire), no Change-model swap, and the
 *  targets table's Share/Strength/$/Mtok columns are wire values or a
 *  physics derivation of them -- never mockups-next/js/routing.js's
 *  client-computed `curW()` or its flat 0.55/cost fixtures. */
export function DeploymentInspector({ dep, cfg, nodes, frame, stale, settings, onClose }: Props) {
  // Same naming rule as the graph and the roster: a machine is called what the
  // operator called it, everywhere, or this sheet's node list stops matching
  // the plates it is describing.
  const name = nameIndex(nodes.map(fromState))
  const depFrame = frame?.deployments.find((d) => d.deployment_id === dep.deployment_id)
  const targets = cfg?.targets ?? []
  const localTarget = targets.find((t) => t.kind === 'local') ?? null
  const counters = localTarget?.counters ?? null
  const decodeTps = counters?.decode_tps ?? null
  const meanDuration = counters?.mean_duration_s ?? null
  const ttft = depFrame?.ttft_ms ?? null

  // A speech or transcription deployment decodes no tokens, so every
  // token-denominated readout below is absent rather than zero.
  const audio = isAudio(dep.modality)
  const degraded = dep.state === 'degraded'
  const serving = dep.state === 'ready' || degraded
  const rate = settings?.electricity_rate_usd_per_kwh ?? 0

  const { backend, invalidate } = useBackend()
  const [stopping, setStopping] = useState(false)
  const [stopError, setStopError] = useState<string | null>(null)
  // Read the disabled state off the wire, not off local `stopping`, so a
  // reload part-way through a stop still shows the truth.
  const alreadyStopping = dep.state === 'stopping' || dep.state === 'stopped'

  const stop = async () => {
    // The consequence, not just the question: this is what the backend
    // actually does -- set_draining, then stop.
    if (
      !window.confirm(
        `Stop ${dep.served_name}? It stops accepting new requests immediately; ` +
          `requests already in flight finish.`,
      )
    )
      return
    setStopping(true)
    setStopError(null)
    try {
      await backend.stopDeployment(dep.deployment_id)
      invalidate()
    } catch (e) {
      setStopError(e instanceof Error ? e.message : String(e))
    } finally {
      setStopping(false)
    }
  }

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: 10 }}>
        <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <Lamp signal={degraded ? 'warn' : serving ? 'live' : 'fault'} label={dep.state} />
          <span className="label mono" style={{ fontSize: 17 }}>
            {dep.served_name}
          </span>
        </span>
        <span style={{ display: 'flex', gap: 8, alignItems: 'baseline' }}>
          {alreadyStopping ? (
            <button disabled>{dep.state === 'stopped' ? 'Stopped' : 'Stopping…'}</button>
          ) : (
            <button onClick={() => void stop()} disabled={stopping}>
              {stopping ? 'Stopping…' : 'Stop'}
            </button>
          )}
          <button onClick={onClose}>Close</button>
        </span>
      </div>
      {stopError ? (
        <p className="label" style={{ color: 'var(--fault)', fontWeight: 400, margin: '8px 0 0' }}>
          {stopError}
        </p>
      ) : null}
      <div className="unit" style={{ margin: '5px 0 0' }}>
        {planShortFromDegrees(dep.plan)} · {dep.runtime}
        {audio ? (
          <> · {dep.modality}</>
        ) : (
          <>
            {' '}
            · {dep.context_length.toLocaleString()} ctx · {dep.max_concurrent_seqs} seqs
          </>
        )}{' '}
        · up {dep.started_at != null ? relativeTime(dep.started_at) : '—'}
      </div>

      {dep.last_error ? (
        <div style={{ marginTop: 8 }}>
          <Verbatim text={dep.last_error} size="label" />
        </div>
      ) : null}

      <div className="sub" style={{ border: 'none', paddingTop: 0 }}>
        latency and throughput
      </div>
      {audio ? (
        <div className="unit">
          Every reading here is denominated in tokens — aggregate and per-stream decode rate, time between
          tokens, time to first token. A {dep.modality} deployment produces audio, so none of them exist for
          it. They are absent rather than zero: the control plane measures no audio-side rate today, and a 0
          would read as a stalled deployment.
        </div>
      ) : (
      <>
      <div className="quad">
        <Quad value={depFrame?.tokens_per_sec ?? null} unit="tok/s aggregate, all streams" />
        <Quad value={decodeTps} unit="tok/s per stream" />
        <Quad value={decodeTps && decodeTps > 0 ? 1000 / decodeTps : null} decimals={1} unit="ms between tokens" />
        <Quad value={ttft} unit="ms to first token" />
        <Quad value={depFrame?.queue_depth ?? null} unit="queued now" />
      </div>
      <div className="unit">
        Aggregate is a trailing-window sum across every stream; per stream is one request&apos;s own decode
        rate, averaged over requests — they answer different questions and only match at concurrency 1. Mean
        request {fmt(meanDuration, 1)} s. All are exponential moving averages; the control plane keeps no
        percentiles. For a non-streaming response there is no first-token boundary, so per-stream decode
        falls back to whole-request duration and silently includes prefill.
      </div>

      <div className="sub">where a request&apos;s time goes</div>
      {ttft != null && meanDuration != null ? (
        <PhaseBar ttftMs={ttft} meanDurationS={meanDuration} />
      ) : (
        <div className="unit">Not yet observed.</div>
      )}
      </>
      )}

      <div className="sub">targets{cfg ? ` · ${cfg.policy.replace(/_/g, ' ')}` : ''}</div>
      {targets.length === 0 ? (
        <div className="unit">—</div>
      ) : (
        <div style={{ overflowX: 'auto' }}>
          <table>
            <thead>
              <tr>
                <th>Target</th>
                <th>Kind</th>
                <th style={{ textAlign: 'right' }}>Share</th>
                <th style={{ textAlign: 'right' }}>In flight</th>
                <th style={{ textAlign: 'right' }}>Strength</th>
                <th style={{ textAlign: 'right' }}>$/Mtok</th>
                <th style={{ textAlign: 'right' }}>Requests</th>
              </tr>
            </thead>
            <tbody>
              {targets.map((t) => (
                <TargetRow key={t.target_id} target={t} nodes={nodes} frame={frame} stale={stale} rate={rate} />
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="sub">placement</div>
      <div className="row">
        <span>Plan</span>
        <span className="mono">{planShortFromDegrees(dep.plan)}</span>
      </div>
      <div className="row">
        <span>Nodes</span>
        <span className="mono">{dep.node_ids.map(name).join(', ') || '—'}</span>
      </div>
      <div className="row">
        <span>Measured all-reduce</span>
        <span className="mono">{dep.node_ids.length >= 2 ? fmtUnit(dep.plan.measured_link_gbps, 1, 'GB/s') : '—'}</span>
      </div>
      <div className="why on" style={{ marginTop: 8 }}>
        <Verbatim text={dep.plan.reason} size="label" />
        {dep.plan.rejected.length > 0 ? (
          <div style={{ marginTop: 6 }}>
            <div className="mut">Rejected</div>
            <VerbatimList items={dep.plan.rejected} />
          </div>
        ) : null}
      </div>
      {dep.fit.warnings.length > 0 ? (
        <div className="why on" style={{ marginTop: 8 }}>
          <VerbatimList items={dep.fit.warnings} />
        </div>
      ) : null}

      <div className="sub">memory on each node</div>
      {dep.node_ids.length === 0 ? (
        <div className="unit">—</div>
      ) : (
        dep.node_ids.map((nodeId) => {
          const n = nodes.find((x) => x.profile.node_id === nodeId)
          if (!n) return null
          const live = nodeLive(n, frame, stale)
          return (
            <div key={nodeId} className="slot">
              <span className="mono unit" style={{ width: 84 }}>
                {name(nodeId)}
              </span>
              <ProportionBar
                value={live.memory_used_pct == null ? null : live.memory_used_pct / 100}
                tone={stale || !live.fresh ? 'muted' : 'ink'}
                label={live.memory_used_pct == null ? `no memory reading for ${nodeId}` : `${pct(live.memory_used_pct)} percent memory used on ${nodeId}`}
              />
              <span className="mono unit" style={{ width: 34, textAlign: 'right' }}>
                {live.memory_used_pct == null ? '—' : `${pct(live.memory_used_pct)}%`}
              </span>
            </div>
          )
        })
      )}
    </div>
  )
}

function Quad({ value, decimals = 0, unit }: { value: number | null; decimals?: number; unit: string }) {
  return (
    <div>
      <div className="readout">{fmt(value, decimals)}</div>
      <div className="unit">{unit}</div>
    </div>
  )
}

function PhaseBar({ ttftMs, meanDurationS }: { ttftMs: number; meanDurationS: number }) {
  const pre = ttftMs / 1000
  const dec = Math.max(0, meanDurationS - pre)
  const tot = pre + dec
  const pp = tot > 0 ? (pre / tot) * 100 : 0
  return (
    <>
      <div className="phase">
        <span style={{ width: `${pp.toFixed(1)}%`, background: 'var(--fill)', color: 'var(--on-fill)' }}>
          {pp >= 12 ? 'prefill' : ''}
        </span>
        <span style={{ width: `${(100 - pp).toFixed(1)}%`, background: 'var(--fill)', opacity: 0.45, color: 'var(--on-fill)' }}>
          decode
        </span>
      </div>
      <div className="legend">
        <span>
          <b>{fmt(pre * 1000, 0)} ms</b> prefill ({pp.toFixed(1)}%)
        </span>
        <span>
          <b>{fmt(dec, 1)} s</b> decode
        </span>
        <span>
          <b>{fmt(tot, 1)} s</b> mean request
        </span>
      </div>
      <div className="unit" style={{ marginTop: 6 }}>
        Prefill processes the whole prompt at once and is compute-bound; decode emits one token at a time and
        is memory-bandwidth-bound. The planner and the fit gate reason about decode bandwidth, not this.
      </div>
    </>
  )
}

function TargetRow({
  target: t,
  nodes,
  frame,
  stale,
  rate,
}: {
  target: RouteTarget
  nodes: NodeStateDTO[]
  frame: SafeMetricsFrame | null
  stale: boolean
  rate: number
}) {
  const decodeTps = t.counters?.decode_tps ?? null
  // Sum every node behind this target, not just the first -- a pipeline
  // target spans more than one node, and pricing off node_ids[0] alone
  // silently under-counts the power actually drawn. (A node shared with
  // another deployment is still priced at its full draw either way: the
  // wire has one power reading per node, not a per-tenant share of it, so
  // that half of the attribution problem has no honest fix without a
  // number nothing measures -- disclosed via the provenance line below,
  // not hidden.)
  const ids = t.node_ids ?? []
  const powerW =
    ids.length === 0
      ? null
      : ids.reduce<number | null>((sum, id) => {
          if (sum == null) return null
          const node = nodes.find((n) => n.profile.node_id === id)
          const w = node ? nodeLive(node, frame, stale).power_w : null
          return w == null ? null : sum + w
        }, 0)
  const derived = t.kind === 'local' ? deriveLocalCost(powerW, decodeTps, rate) : null
  const published = t.cost_per_mtok
  const cost = published ?? derived

  return (
    <tr>
      <td className="mono">{targetLabel(t)}</td>
      <td className="unit">{t.kind}</td>
      <td className="num">{pct(t.weight * 100)}%</td>
      <td className="num">{fmt(t.outstanding, 0)}</td>
      <td className="num">
        {t.strength_raw != null && t.strength_source ? (
          <>
            {t.strength_raw.toFixed(2)} <span className="unit">{t.strength_source}</span>
          </>
        ) : (
          '—'
        )}
      </td>
      <td className="num">
        {cost != null ? (
          <>
            ${cost.toFixed(3)}
            {published == null && derived != null ? (
              <div className="unit">
                from {fmt(powerW, 0)} W at {fmt(decodeTps, 0)} tok/s · ${rate.toFixed(2)}/kWh
              </div>
            ) : null}
          </>
        ) : (
          <>
            —
            {t.kind === 'local' && rate <= 0 ? (
              <div className="unit">set an electricity rate to price local generation</div>
            ) : null}
          </>
        )}
      </td>
      <td className="num">{fmt(t.counters?.completed ?? null, 0)}</td>
    </tr>
  )
}
