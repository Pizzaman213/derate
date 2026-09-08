import type {
  FitResult,
  PlanResponse,
  ServeRequirement,
  Verdict as VerdictWord,
} from '../../api/types'
import { Lamp } from '../../components/Lamp'
import { Readout } from '../../components/Readout'
import { SegmentBar, type Segment } from '../../components/Bars'
import { Verbatim, VerbatimList } from '../../components/Verbatim'
import { OverrideGate } from '../../components/OverrideGate'
import { gbNum, gbytes, planShortFromDegrees } from '../../format'

interface Props {
  result: PlanResponse
  checking: boolean
  error: string | null
  context: number
  onUseMaxContext: (c: number) => void
  onLaunch: () => void
  launching: boolean
  /** Every permission this launch needs, already resolved from `serve` by
   *  ServePanel (which also has to send them). */
  gates: ServeRequirement[]
  /** Which of them have been ticked. Owned by ServePanel so it can be cleared
   *  on every replan: an override must never outlive the number it was granted
   *  against. */
  granted: Record<string, boolean>
  onGrant: (param: string, value: boolean) => void
  /** A full custom launch command, replacing the one this plan would
   *  otherwise build, and the checkbox that gates whether it is actually
   *  sent. Owned by `ServePanel`, not this component: it also decides what
   *  gets recorded to state/customServes.ts, and a second copy of either
   *  value here could disagree with the one that launch() reads. */
  customCommandText: string
  onCustomCommandTextChange: (value: string) => void
  customCommand: boolean
  onCustomCommandChange: (value: boolean) => void
}

const VERDICT_COPY: Record<VerdictWord, { word: string; signal: 'live' | 'warn' | 'fault' }> = {
  fits: { word: 'fits', signal: 'live' },
  fits_degraded: { word: 'fits, degraded', signal: 'warn' },
  wont_fit: { word: 'will not fit', signal: 'fault' },
}

/** The dry run, and the refusal, in the one box mockups-next/js/planner.js
 *  draws as `#verdict`. Nothing here computes a fit; it renders the two the
 *  fit gate already computed -- one against the static ceiling, one against
 *  what the machine can actually hand out right now -- and lets the backend's
 *  `serve` decision say which of them governs the button. */
export function Verdict({
  result, checking, error, context, onUseMaxContext, onLaunch, launching, gates, granted, onGrant,
  customCommandText, onCustomCommandTextChange, customCommand, onCustomCommandChange,
}: Props) {
  const p = result.plan
  const fit = result.fit
  const live = result.fit_live ?? null
  const serve = result.serve ?? null

  // The fit port can be unwired, in which case there is no verdict at all.
  // Rendering the plan without one is honest; pretending it fits is not.
  if (!fit && !live) return <NoVerdict result={result} />

  // The breakdown is identical under both budgets, so either carries it.
  const shown = live ?? fit!
  const segments: Segment[] = [
    { key: 'weights', label: 'weights', bytes: shown.breakdown.weights },
    { key: 'kv_cache', label: 'kv cache', bytes: shown.breakdown.kv_cache },
    { key: 'activations', label: 'activations', bytes: shown.breakdown.activations },
    { key: 'comm_buffers', label: 'comm buffers', bytes: shown.breakdown.comm_buffers },
    { key: 'replicated', label: 'replicated', bytes: shown.breakdown.replicated },
    { key: 'framework_overhead', label: 'framework overhead', bytes: shown.breakdown.framework_overhead },
  ]
    .filter((s) => s.bytes > 0)
    .map((s) => ({ ...s, limiting: s.key === shown.limiting_term }))

  // Serve is the backend's call when the backend makes one. A gateway that
  // predates `serve` omits the field entirely, and treating that absence as a
  // refusal would red-flag every model on an older coordinator -- so fall
  // back to the static verdict, which is exactly what such a gateway means.
  const legacy = serve == null
  const fitPassed = legacy ? fit != null && fit.verdict !== 'wont_fit' : serve.allowed === true
  // A refusal with nothing on offer: no button, just the reason. A static
  // WONT_FIT is this -- the model does not fit the hardware at all, and no
  // tick produces a launch.
  const refusedOutright = !fitPassed && gates.length === 0
  // Serve appears once every gate on offer has been ticked. With no gates
  // (the normal case) this is `fitPassed`, exactly as before.
  const canServe = !refusedOutright && gates.every((g) => granted[g.param] === true)
  const needsOverride = gates.length > 0 && !canServe

  // Prefer the live figure: the most context that fits on an idle machine is
  // not an offer anybody can act on right now.
  const maxCtx = live?.max_context_that_fits ?? fit?.max_context_that_fits ?? null
  const canAdjust = maxCtx != null && maxCtx > 0 && maxCtx !== context

  return (
    <div className={`verdict on${canServe ? '' : ' bad'}`} style={{ opacity: checking ? 0.6 : 1 }}>
      {/* A custom launch command, drawn onto the card's own top edge rather
          than as a control floating above it -- a filing-cabinet tab fused
          into the card, not a settings toggle sitting on it. Two-way switch
          rather than a checkbox: the same "one of two modes" shape as the
          Models list's cards/rows toggle, just its own tab styling instead
          of `.mview`'s pill, because this one has to merge with the border
          it sits on. Switching back to Basic disables the tokens without
          discarding them, so a value typed here survives a moment's
          hesitation. */}
      <div className="verdict-modetabs" role="radiogroup" aria-label="Launch command mode">
        <button
          type="button"
          role="radio"
          aria-checked={!customCommand}
          aria-pressed={!customCommand}
          onClick={() => onCustomCommandChange(false)}
        >
          Basic
        </button>
        <button
          type="button"
          role="radio"
          aria-checked={customCommand}
          aria-pressed={customCommand}
          onClick={() => onCustomCommandChange(true)}
        >
          Advanced
        </button>
      </div>
      {customCommand ? (
        <div style={{ display: 'grid', gap: 6, margin: '10px 0' }}>
          <input
            id="verdict-custom-command"
            className="mono"
            type="text"
            placeholder="--tensor-parallel-size 2 --gpu-memory-utilization 0.85 --max-model-len 32768 --trust-remote-code"
            value={customCommandText}
            onChange={(e) => onCustomCommandTextChange(e.target.value)}
          />
          <p className="unit" style={{ margin: 0 }}>
            Replaces the command below entirely — the TP, PP, context,
            concurrency and memory figures on this card become informational
            only; only what you type here is sent. --host, --port and
            --served-model-name still get pinned to what the gateway needs
            after it, no matter what you write. Each token is checked
            against a safety allowlist server-side; a rejected one is
            refused with the reason named.
          </p>
        </div>
      ) : null}

      <div className="vhead" style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 8 }}>
        <span>{planShortFromDegrees(p)}</span>
        <Readout value={p.measured_link_gbps} decimals={1} width={5} unit="GB/s link" />
      </div>

      {/* The planner's argument against the shape that was chosen anyway,
          whole. The mockup abbreviated this to "link 10.2 GB/s below 40 GB/s
          threshold"; the real line names the exchange count and the bytes per
          step, and it is the sentence that says WHY this is likely to be
          wrong. Rendering planner strings entire is structural here, so the
          frame is the mockup's and the words are the planner's. */}
      {result.degrees?.rejection ? (
        <div className="swapbar on" style={{ alignItems: 'flex-start' }}>
          <span aria-hidden className="mono" style={{ color: 'var(--warn)' }}>
            ⚠
          </span>
          <span style={{ display: 'grid', gap: 4 }}>
            <Verbatim text={result.degrees.rejection} size="label" />
            <span className="label" style={{ fontWeight: 500, color: 'var(--warn)' }}>
              Overruling.
            </span>
          </span>
        </div>
      ) : null}

      {/* Two verdicts, each with its own lamp. The static row never says a
          bare "fits": an unqualified "fits" is the claim that let a launch
          through onto a machine that had no room for it. */}
      <div style={{ display: 'grid', gap: 4, margin: '6px 0 10px' }}>
        {fit ? (
          <VerdictRow
            label="fits on idle hardware"
            fit={fit}
            figure={`${gbytes(fit.usable_per_node, 1)} GB ceiling`}
            muted={live != null}
          />
        ) : null}
        {live ? (
          <VerdictRow
            label="fits right now"
            fit={live}
            figure={`${gbytes(live.usable_per_node, 1)} GB allocatable`}
          />
        ) : serve?.unavailable_reason ? (
          <p className="unit" style={{ margin: 0 }}>
            {`No live memory reading: ${serve.unavailable_reason}. Only the idle-hardware answer is available.`}
          </p>
        ) : null}
      </div>

      <Verbatim text={p.reason} size="label" />

      {p.rejected.length > 0 ? (
        <div style={{ display: 'grid', gap: 6, paddingTop: 6 }}>
          <div className="unit">rejected</div>
          <VerbatimList items={p.rejected} />
        </div>
      ) : null}

      <div className="unit" style={{ margin: '6px 0 10px' }}>
        {p.node_ids.length} {p.node_ids.length === 1 ? 'machine' : 'machines'}: {p.node_ids.join(', ')}
        {/* The line already names the machines; whose choice they were is the
            other half of the fact. */}
        {result.placement?.mode === 'operator' ? ' · chosen by you' : ''}
        {result.placement?.unused_node_ids?.length
          ? ` · ${result.placement.unused_node_ids.join(', ')} carries no rank`
          : ''}
      </div>

      {result.resolver_warnings.length > 0 ? (
        <div style={{ display: 'grid', gap: 6, margin: '6px 0 10px' }}>
          <div className="unit">resolver</div>
          <VerbatimList items={result.resolver_warnings} />
        </div>
      ) : null}

      {/* Both reasons, each under its own legend, neither merged into the
          other. They are describing different budgets. */}
      {fit && live && fit.reason !== live.reason ? (
        <div style={{ display: 'grid', gap: 8 }}>
          <div>
            <div className="unit">on idle hardware</div>
            <Verbatim text={fit.reason} size="label" />
          </div>
          <div>
            <div className="unit">right now</div>
            <Verbatim text={live.reason} size="label" />
          </div>
        </div>
      ) : (
        <Verbatim text={(live ?? fit!).reason} size="label" />
      )}

      {/* One bar, both lines: the live allocatable line is what the verdict
          used, the static ceiling sits behind it. */}
      <div style={{ display: 'grid', gap: 8, marginTop: 8 }}>
        <SegmentBar
          segments={segments}
          usable={(live ?? fit)?.usable_per_node ?? null}
          ceiling={live ? fit?.usable_per_node ?? null : null}
        />
        <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8 }}>
          <span className="unit">per node</span>
          <span className="unit">
            {live
              ? `line at ${gbytes(live.usable_per_node, 1)} GB allocatable · ceiling ${gbytes(fit?.usable_per_node ?? 0, 1)} GB`
              : `usable line at ${gbytes(fit!.usable_per_node, 1)} GB`}
          </span>
        </div>
      </div>

      <dl
        style={{
          margin: '8px 0 0',
          display: 'grid',
          gridTemplateColumns: 'max-content max-content max-content',
          columnGap: 16,
          rowGap: 4,
          alignItems: 'baseline',
        }}
      >
        {segments.map((s) => (
          <BreakdownRow key={s.key} label={s.label} bytes={s.bytes} limiting={s.limiting === true} />
        ))}
        <div style={{ gridColumn: '1 / -1', borderTop: '1px solid var(--rule)', margin: '4px 0' }} />
        <BreakdownRow label="total" bytes={shown.breakdown.total} strong />
        {fit ? (
          <BreakdownRow label="ceiling on idle hardware" bytes={fit.usable_per_node} muted={live != null} />
        ) : null}
        {live ? <BreakdownRow label="allocatable right now" bytes={live.usable_per_node} /> : null}
        {fit ? (
          <BreakdownRow
            label={live ? 'headroom on idle hardware' : 'headroom'}
            bytes={fit.headroom}
            muted={live != null}
            tone={fit.headroom < 0 ? 'fault' : undefined}
          />
        ) : null}
        {live ? (
          <BreakdownRow
            label="headroom right now"
            bytes={live.headroom}
            tone={live.headroom < 0 ? 'fault' : undefined}
          />
        ) : null}
      </dl>

      {shown.predicted_decode_tps != null ? (
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginTop: 8 }}>
          <span className="unit">predicted decode</span>
          <Readout
            value={shown.predicted_decode_tps}
            decimals={1}
            width={5}
            unit="tok/s"
            tone={shown.verdict === 'fits_degraded' ? 'warn' : 'ink'}
          />
        </div>
      ) : null}

      {shown.warnings.length > 0 ? (
        <div style={{ display: 'grid', gap: 6, marginTop: 8 }}>
          {shown.warnings.map((w) => (
            <p key={w} className="label" style={{ margin: 0, fontWeight: 400, color: 'var(--warn)', whiteSpace: 'pre-wrap' }}>
              {w}
            </p>
          ))}
        </div>
      ) : null}

      <div style={{ display: 'grid', gap: 10, marginTop: 10 }}>
        {canServe ? (
          <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
            <button onClick={onLaunch} disabled={launching}>
              {launching
                ? 'Launching…'
                : gates.length > 0
                  ? 'Serve anyway'
                  : 'Serve'}
            </button>
            {shown.verdict === 'fits_degraded' && shown.predicted_decode_tps != null ? (
              <span className="label" style={{ fontWeight: 400, color: 'var(--warn)' }}>
                It will load and decode at {shown.predicted_decode_tps.toFixed(1)} tok/s.
              </span>
            ) : null}
          </div>
        ) : needsOverride ? (
          <div style={{ display: 'grid', gap: 12 }}>
            {gates.map((g) => (
              <OverrideGate
                key={g.param}
                reason={g.reason}
                sentence={claimFor(g, live, fit?.usable_per_node ?? null, p)}
                checked={granted[g.param] === true}
                onChange={(v) => onGrant(g.param, v)}
              />
            ))}
            {/* No button until every box is ticked -- the override stays a
                second, deliberate act, which is the whole point of the gate.
                Once they all are, `canServe` flips and the Serve button in the
                branch above renders, labelled "Serve anyway". With more than
                one gate the count says how far along you are, because
                otherwise ticking the first box appears to do nothing. */}
            {gates.length > 1 ? (
              <span className="unit">
                {gates.filter((g) => granted[g.param] === true).length} of{' '}
                {gates.length} agreed
              </span>
            ) : null}
          </div>
        ) : (
          <p className="label" style={{ margin: 0, fontWeight: 400, color: 'var(--fault)' }}>
            {serve?.reason ?? fit?.reason ?? 'This will not fit.'}
          </p>
        )}

        <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
          {/* The gate said what to change; this is that change, already applied. */}
          {canAdjust ? (
            <button onClick={() => onUseMaxContext(maxCtx!)}>
              Use {maxCtx} context{live ? ' — the most that fits right now' : ''}
            </button>
          ) : null}

          {!canServe && !needsOverride && !canAdjust ? (
            <span className="label muted" style={{ fontWeight: 400 }}>
              No context length makes this fit on the current machines.
            </span>
          ) : null}
        </div>
      </div>

      {/* The refusal, verbatim, when Serve itself was rejected rather than
          the dry run. */}
      {error ? (
        <p className="label" style={{ color: 'var(--fault)', fontWeight: 400, margin: '10px 0 0' }}>
          {error}
        </p>
      ) : null}
    </div>
  )
}

function VerdictRow({
  label,
  fit,
  figure,
  muted,
}: {
  label: string
  fit: FitResult
  figure: string
  muted?: boolean
}) {
  const v = VERDICT_COPY[fit.verdict]
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'baseline',
        justifyContent: 'space-between',
        gap: 8,
        color: muted ? 'var(--ink-muted)' : 'var(--ink)',
      }}
    >
      <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <Lamp signal={v.signal} hollow={muted} label={`${label}: ${v.word}`} />
        <span className="label" style={{ fontWeight: 400 }}>
          {label}
        </span>
      </span>
      <span style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
        <span className="label" style={{ fontWeight: muted ? 400 : 500 }}>
          {v.word}
        </span>
        <span className="unit">{figure}</span>
      </span>
    </div>
  )
}

/** A checkbox whose label IS the claim, not a button.
 *
 *  `window.confirm` is this codebase's destructive-action pattern (see
 *  NodesCard). This is not destructive -- it is an assertion about a machine,
 *  and the honest shape for one is a sentence you have to read to reach the
 *  button. Every figure in it comes off the wire, so it changes as the
 *  machine does. */
/** What ticking a given box means, in the first person, naming the figures it
 *  waives.
 *
 *  Composed here rather than sent by the server on purpose: it is a claim the
 *  operator is making, not a message they are dismissing, and it reads that way
 *  only if it is written in their voice. The server's own sentence renders
 *  verbatim directly above it either way.
 *
 *  The default branch matters as much as the named ones. A gateway that adds a
 *  fourth gate tomorrow still gets an honest, blocking control here instead of
 *  this client silently launching past a permission it did not recognise. */
function claimFor(
  gate: ServeRequirement,
  live: FitResult | null,
  staticCeiling: number | null,
  plan: PlanResponse['plan'],
): string {
  if (gate.param === 'allow_over_live_memory') {
    return live
      ? `Serve anyway. I am overriding the live fit gate, which measured ` +
          `${gbytes(live.usable_per_node, 1)} GB allocatable and refused` +
          (staticCeiling != null
            ? `. The ${gbytes(staticCeiling, 1)} GB static ceiling is not reachable right now.`
            : '.')
      : `Serve anyway. There is no live memory reading, so nothing has checked ` +
          `whether this fits on the machine as it is right now — only on idle hardware.`
  }
  if (gate.param === 'allow_mixed_hardware') {
    return (
      `Pool them anyway. I am putting ${plan.node_ids.join(' and ')} in one ` +
      `deployment even though they are not alike, and every request will run ` +
      `at the speed of the slowest one.`
    )
  }
  return 'Launch anyway. I have read the reason above and am overriding it.'
}

/** No verdict at all. The plan is real and worth showing; the absence of a
 *  fit is stated rather than papered over, and Serve is withheld. */
function NoVerdict({ result }: { result: PlanResponse }) {
  const p = result.plan
  return (
    <div className="verdict on bad">
      <div className="vhead">
        <Lamp signal="fault" label="no verdict" /> {planShortFromDegrees(p)}
      </div>
      <Verbatim text={p.reason} size="label" />
      <p className="label" style={{ fontWeight: 400, margin: '10px 0 0' }}>
        The fit gate did not answer, so nothing here says whether this fits.
        Serve is withheld.
      </p>
      {result.serve?.unavailable_reason ? (
        <p className="unit" style={{ margin: '6px 0 0' }}>{result.serve.unavailable_reason}</p>
      ) : null}
    </div>
  )
}

function BreakdownRow({
  label,
  bytes,
  limiting,
  strong,
  muted,
  tone,
}: {
  label: string
  bytes: number
  limiting?: boolean
  strong?: boolean
  muted?: boolean
  tone?: 'fault'
}) {
  return (
    <>
      <dt
        className="label"
        style={{
          fontWeight: strong ? 500 : 400,
          color: strong ? 'var(--ink)' : 'var(--ink-muted)',
          opacity: muted ? 0.72 : 1,
        }}
      >
        {label}
      </dt>
      <dd style={{ margin: 0, opacity: muted ? 0.72 : 1 }}>
        <Readout value={gbNum(bytes)} decimals={1} width={7} unit="GB" tone={tone ?? 'ink'} />
      </dd>
      <dd style={{ margin: 0 }}>
        {limiting ? (
          <span className="label" style={{ fontWeight: 400, color: 'var(--ink)' }}>
            limiting
          </span>
        ) : null}
      </dd>
    </>
  )
}
