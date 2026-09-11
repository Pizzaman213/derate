import { useState } from 'react'
import { isAudio } from '../api/types'
import type {
  DeploymentDTO,
  NodeStateDTO,
  RouteTarget,
  RoutingConfig,
  Settings,
  SpeculativeCounters,
} from '../api/types'
import { useBackend } from '../state/backend'
import type { SafeMetricsFrame } from '../state/useMetrics'
import { Lamp } from '../components/Lamp'
import { ProportionBar } from '../components/Bars'
import { Verbatim, VerbatimList } from '../components/Verbatim'
import { nodeLive } from '../state/live'
import { fromState, nameIndex } from '../state/names'
import { useActivity } from '../state/resources'
import { LAUNCH_PHASES, phaseLabel, phaseRank } from '../state/launchPhase'
import { DeploymentLog } from './DeploymentLog'
import { fmt, fmtUnit, gbytes, pct, planShortFromDegrees, relativeTime, remainingLabel } from '../format'

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

/** What a launch is doing, on the sheet that is open on that launch.
 *
 *  This replaces the latency and throughput block while a deployment is still
 *  arriving, because that block had nothing to say about one: `0 tok/s
 *  aggregate`, `— tok/s per stream`, `— ms to first token`, `0 queued now`
 *  and "Not yet observed." are five ways of reporting silence about a model
 *  that is in fact busy, for up to half an hour, doing four different things
 *  in sequence. The two zeros were the worst of it -- a deployment that has
 *  not started cannot be serving at zero tokens a second, and nothing on the
 *  sheet said which of "starting" and "stalled" it was looking at.
 *
 *  Everything below is read, not inferred: the server classifies the launch
 *  from sparkrun's output and the backend's own log
 *  (`control_plane/deploy/progress.py`) and this draws what it says. The
 *  ladder is the same one the first-run wizard walks, from the same module,
 *  so the two screens cannot drift into two vocabularies.
 *
 *  Mounted only while the deployment is arriving, which is what keeps
 *  `/api/activity` off this sheet for the READY case that is most of them.
 */
function Starting({ dep }: { dep: DeploymentDTO }) {
  const activity = useActivity()
  const row = (activity.data?.launches ?? []).find(
    (l) => l.deployment_id === dep.deployment_id,
  )
  const at = phaseRank(row?.phase)
  const fraction =
    row?.fraction != null && Number.isFinite(row.fraction)
      ? Math.max(0, Math.min(1, row.fraction))
      : null
  const remaining = remainingLabel(row?.eta_s)

  return (
    <>
      <div className="sub" style={{ border: 'none', paddingTop: 0 }}>
        what it is doing
      </div>

      {/* Filled only while the runtime is counting its own checkpoint shards.
          The pull, the download, the compile and the graph capture report no
          denominator, so the track is drawn open for them rather than filled
          at a rate this screen would have had to invent. */}
      <ProportionBar
        value={fraction}
        tone={row?.fatal ? 'fault' : 'ink'}
        label={
          fraction == null
            ? `${dep.served_name} is starting, with no progress figure to report`
            : `${Math.round(fraction * 100)} percent of ${dep.served_name} loaded onto the GPU`
        }
      />

      <ul
        style={{
          listStyle: 'none',
          margin: '8px 0 0',
          padding: 0,
          display: 'grid',
          gap: 6,
        }}
      >
        {LAUNCH_PHASES.map((phase, i) => {
          // Reached, current, still ahead. When the server has not named a
          // phase yet, `at` is -1 and every step is ahead -- which is true,
          // and better than ticking one on the assumption that a launch that
          // has said nothing must at least have started.
          const state = at < 0 ? 'todo' : i < at ? 'done' : i === at ? 'now' : 'todo'
          return (
            <li
              key={phase}
              className="label"
              style={{
                fontWeight: state === 'now' ? 500 : 400,
                color: state === 'todo' ? 'var(--ink-muted)' : 'var(--ink)',
                display: 'grid',
                gridTemplateColumns: '10px 1fr',
                gap: 6,
              }}
            >
              <span aria-hidden="true" className="mono">
                {state === 'done' ? '✓' : state === 'now' ? '●' : '○'}
              </span>
              <span>{phaseLabel(phase)}</span>
            </li>
          )
        })}
      </ul>

      {/* sparkrun's line or the runtime's, exactly as it arrived: "Pulling
          image: ghcr.io/...", "Loading safetensors checkpoint shards: 5/11",
          "Capturing CUDA graphs". More specific than the step above it, and
          the only thing on the sheet that distinguishes one four-minute
          silence from another. */}
      {row?.status ? (
        <div style={{ marginTop: 8 }}>
          <Verbatim text={row.status} size="unit" />
        </div>
      ) : null}

      <div className="unit" style={{ marginTop: 6 }}>
        {row
          ? // Not "launching for 4m": `since` is when this coordinator first
            // saw it, and after a restart that is when the coordinator came
            // back rather than when the launch began.
            `Seen starting ${relativeTime(row.since)}. `
          : 'Nothing has been read from this launch yet. '}
        {/* The estimate, when there is one, said out loud with whose it is.
            Two of the four steps count themselves -- the downloader and the
            checkpoint loader are both progress bars and both print their own
            remaining time -- and the other two report no total at all, so
            they get silence rather than a number this screen extrapolated. */}
        {remaining ? (
          <>
            <strong style={{ fontWeight: 500 }}>{remaining}</strong>, by the{' '}
            {row?.phase === 'downloading' ? 'downloader' : 'runtime'}&rsquo;s own count
            of the step it is on — not of the whole launch.{' '}
          </>
        ) : (
          <>
            No estimate: nothing reports a total for this step.{' '}
          </>
        )}
        The first launch of a model on a machine is the slow one: the runtime
        container and the weights are fetched once, and the engine compiles and
        captures CUDA graphs into a cache that later launches reuse.
      </div>
    </>
  )
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
  const spec = depFrame?.speculative ?? null

  // A speech or transcription deployment decodes no tokens, so every
  // token-denominated readout below is absent rather than zero.
  const audio = isAudio(dep.modality)
  const degraded = dep.state === 'degraded'
  const serving = dep.state === 'ready' || degraded
  // On its way in. The same allowlist /api/activity uses, and for the same
  // reason it is an allowlist: DEGRADED is up and serving badly, STOPPING is
  // leaving, and neither is arriving.
  const arriving = dep.state === 'planned' || dep.state === 'launching'
  const rate = settings?.electricity_rate_usd_per_kwh ?? 0

  const { backend, invalidate } = useBackend()
  const [stopping, setStopping] = useState(false)
  const [stopError, setStopError] = useState<string | null>(null)
  // Read the disabled state off the wire, not off local `stopping`, so a
  // reload part-way through a stop still shows the truth.
  const alreadyStopping = dep.state === 'stopping' || dep.state === 'stopped'

  // Absent from a gateway that predates the field; read it as on, which is
  // what every deployment then was.
  const offered = dep.serving !== false
  const [switching, setSwitching] = useState(false)

  const setOffered = async (next: boolean) => {
    setSwitching(true)
    setStopError(null)
    try {
      await backend.setDeploymentServing(dep.deployment_id, next)
      invalidate()
    } catch (e) {
      setStopError(e instanceof Error ? e.message : String(e))
    } finally {
      setSwitching(false)
    }
  }

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
          {/* Arriving is amber, not red. A launch drew the fault lamp for the
              whole of its startup because the expression only knew "serving"
              and "not serving" -- so a model doing exactly what it was asked
              to do, for up to half an hour, was reported as broken, beside a
              stop button. The rail has always drawn this state as warn. */}
          <Lamp
            signal={degraded || arriving || dep.state === 'stopping' ? 'warn' : serving ? 'live' : 'fault'}
            label={dep.state}
          />
          <span className="label mono" style={{ fontSize: 17 }}>
            {dep.served_name}
          </span>
          {dep.origin === 'adopted' ? (
            <span
              className="pill"
              title="This container was already running and unrecorded -- derate never launched it. The plan and fit shown below are reconstructed from its running flags, not decided in advance."
            >
              Adopted
            </span>
          ) : null}
        </span>
        <span style={{ display: 'flex', gap: 8, alignItems: 'baseline' }}>
          {/* Offered on the API, or not. Deliberately NOT a stop: the
              container stays up and keeps its GPU memory, which is the whole
              difference between the two and is said out loud below. Only
              while the thing is actually up -- there is nothing to take off
              /v1/models once it has stopped. */}
          {!alreadyStopping && serving ? (
            <button onClick={() => void setOffered(!offered)} disabled={switching}>
              {switching ? 'Saving…' : offered ? 'Take off the API' : 'Put back on the API'}
            </button>
          ) : null}
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
      {/* The cost of the switch, in the one unit that matters on this
          hardware. A deployment taken off the API is still resident: it holds
          every byte the fit gate charged it, and nothing can reach it. Saying
          only "off the API" would make an idle 30 GiB invisible, which is the
          opposite of what this whole product is for. */}
      {!offered && serving ? (
        <p
          className="unit"
          style={{ color: 'var(--warn)', margin: '8px 0 0', fontWeight: 400 }}
        >
          Off the API — not in /v1/models, not routed, not in the chat picker.
          The container is still running and still holding
          {dep.fit?.breakdown
            ? ` ${gbytes(dep.fit.breakdown.weights + dep.fit.breakdown.kv_cache)} GiB per rank`
            : ' its GPU memory'}
          . Stop it to get that back.
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

      {/* Two columns. Left is the deployment; right is its backend log, which
          was a disclosure under all of this and is now beside it -- during a
          launch the log IS the sheet, and it was the one thing below the fold.
          The column is sticky, so it holds while this one scrolls. */}
      <div className="depgrid" style={{ marginTop: 12 }}>
        <div>
        {arriving ? <Starting dep={dep} /> : (
        <>
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

        {/* Only for a deployment that is actually speculating. Absent is the
            ordinary case and is deliberately silent rather than an empty
            section reading zero: a model with no draft head has no acceptance
            rate, and the fit gate's own sentence about the rate being
            unmeasured is still the truth for it. */}
        {spec ? <SpeculativeBlock spec={spec} /> : null}
        </>
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

        <aside className="deplogcol">
          <DeploymentLog
            deploymentId={dep.deployment_id}
            // Free to show whenever the coordinator is still following this
            // deployment, which is every state but a quietly stopped one. A
            // stopped one costs a `sparkrun logs` on the machine, so it is
            // asked for -- unless it left a reason behind, which is exactly
            // when somebody opened this sheet to read the log.
            autoOpen={dep.state !== 'stopped' || dep.last_error != null}
          />
        </aside>
      </div>
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


/** What the engine itself counted about its speculation, over one scrape
 *  window of the coordinator's own slower clock.
 *
 *  This is the number the Verdict card says it does not have. That card
 *  states a floor and a ceiling and a sentence saying the acceptance rate
 *  between them is not measured -- true at PLAN time, when there is no engine
 *  to ask. Once one is running there is, and this is it. The two are not in
 *  conflict and neither replaces the other: the range is what is true for a
 *  workload nobody has run, this is what happened on the workload that ran.
 *
 *  Deliberately not fed back into routing. `strength.py` scores targets from
 *  real proxied traffic, and an acceptance figure -- however measured -- must
 *  not outrank that. */
function SpeculativeBlock({ spec }: { spec: SpeculativeCounters }) {
  return (
    <>
      <div className="sub">speculative decoding · measured</div>
      <div className="quad">
        <Quad value={spec.acceptance * 100} decimals={1} unit="% of drafted tokens kept" />
        <Quad value={spec.accepted_per_step} decimals={2} unit="drafted tokens per step" />
        <Quad value={spec.drafts} unit="draft rounds" />
        <Quad value={spec.draft_tokens} unit="tokens proposed" />
      </div>
      {/* The shape, not just the mean. A head that lands position 0 almost
          always and position 3 almost never is a head to run at a lower n --
          and averaging the positions together hides exactly that, which is
          the decision this table exists to inform. */}
      {spec.acceptance_per_pos.length > 0 ? (
        <div style={{ display: 'grid', gap: 4, marginTop: 6 }}>
          {spec.acceptance_per_pos.map((v, i) => (
            <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <span className="unit" style={{ minWidth: 72 }}>
                position {i}
              </span>
              {/* `v` straight through, never `v ?? 0`: the bar draws a null as
                  a dashed empty track and a real zero as a solid one, and
                  collapsing the two would make "never accepted here" and "no
                  reading for this position" pixel-identical. */}
              <ProportionBar value={v} width={140} label={`position ${i} acceptance`} />
              <span className="unit" style={{ minWidth: 48, textAlign: 'right' }}>
                {/* pct() wants a 0..100 scale and these are fractions. */}
                {v == null ? '—' : `${pct(v * 100)}%`}
              </span>
            </div>
          ))}
        </div>
      ) : null}
      <div className="unit">
        Read from the engine&apos;s own <span className="mono">vllm:spec_decode_*</span> counters and
        differenced over the last scrape window, so it is what just happened rather than the
        engine&apos;s whole life. Position acceptance is cumulative, not conditional: a draft is only
        checked at position 2 when position 1 was accepted first, which is why the figures fall away
        rather than varying freely. Nothing here feeds routing.
      </div>
    </>
  )
}
