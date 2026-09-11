import { useState, type ReactNode } from 'react'
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
import { Disclosure } from '../../components/Panel'
import { gbNum, gbytes, planShortFromDegrees } from '../../format'

interface Props {
  result: PlanResponse
  checking: boolean
  error: string | null
  context: number
  /** What the verdict's concurrency was actually taken at, on the same terms
   *  as `context` -- used only to name a concrete number in the CUDA graph
   *  capture hint below, since "capture up to your own concurrency" means
   *  nothing without it. Null is a legitimate answer (no verdict yet), not an
   *  error -- the hint just falls back to generic wording. */
  concurrency: number | null
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
  /** Skip CUDA graph capture and torch.compile entirely, trading decode
   *  throughput for a faster launch. Owned by `ServePanel`, on the same
   *  terms as the custom-command pair above: it also decides what actually
   *  gets sent on launch. Mutually exclusive with `cudagraphSizesText`
   *  (nothing left to trim once graphs are off) and, like both of those,
   *  with `customCommand`. */
  enforceEager: boolean
  onEnforceEagerChange: (value: boolean) => void
  /** The KV cache element width, or '' for the coordinator's default. Unlike
   *  every other control in this card it changes the verdict this card is
   *  showing -- the panel re-plans on it, because fp8 halves bytes-per-token
   *  and so changes the context the gate approves. */
  kvDtype: string
  forcedQuant: string
  onForcedQuantChange: (value: string) => void
  onKvDtypeChange: (value: string) => void
  /** Raw text: space- or comma-separated batch sizes to capture CUDA graphs
   *  for, instead of the runtime's own default list. Parsed into
   *  `cudagraph_capture_sizes` by `ServePanel` at launch time, exactly like
   *  `customCommandText` is tokenized there rather than here. */
  cudagraphSizesText: string
  onCudagraphSizesTextChange: (value: string) => void
  /** The quantization ladder, folded into this card as its own labeled
   *  subsection rather than a second `.verdict` box stacked below it. Owned
   *  and rendered by the caller (`ServePanel`) -- this component only decides
   *  where it sits, never what it says. */
  quantSection?: ReactNode
  /** The speculative-decoding picker, owned and rendered by `ServePanel` on
   *  exactly the same terms as `quantSection`: this component decides where it
   *  sits and never what it says.
   *
   *  It sits HERE, under `predicted decode`, rather than in the panel's
   *  advanced disclosure where it started. Speculative decoding is a modifier
   *  on that one number, and a control that changes a figure belongs beside
   *  the figure it changes -- behind a disclosure it was a feature you had to
   *  already know about to find. */
  speculativeSection?: ReactNode
}

/** An example capture list that never suggests a size above `concurrency`
 *  itself -- doubling from 1 up to it, then the exact value, so a
 *  concurrency of 1 reads as just "1" rather than trailing off into sizes
 *  the very sentence beside it says are pointless. Illustrative only: never
 *  parsed, never sent, just what the placeholder shows. */
function cudagraphSizeHint(concurrency: number): string {
  const sizes: number[] = []
  for (let n = 1; n < concurrency; n *= 2) sizes.push(n)
  sizes.push(concurrency)
  return sizes.join(' ')
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
  result, checking, error, context, concurrency, onUseMaxContext, onLaunch, launching, gates, granted, onGrant,
  customCommandText, onCustomCommandTextChange, customCommand, onCustomCommandChange, quantSection,
  speculativeSection, enforceEager, onEnforceEagerChange, kvDtype, onKvDtypeChange,
  forcedQuant, onForcedQuantChange,
  cudagraphSizesText, onCudagraphSizesTextChange,
}: Props) {
  const p = result.plan
  const fit = result.fit
  const live = result.fit_live ?? null
  const serve = result.serve ?? null
  // The granular breakdown is the densest, most jargon-heavy part of this
  // card and the least often what decides anything -- total and headroom do
  // that. Collapsed by default; nothing in it is paraphrased or removed, only
  // deferred a click.
  const [breakdownOpen, setBreakdownOpen] = useState(false)

  // The fit port can be unwired, in which case there is no verdict at all.
  // Rendering the plan without one is honest; pretending it fits is not.
  if (!fit && !live)
    return (
      <NoVerdict
        result={result}
        quantSection={quantSection}
        speculativeSection={speculativeSection}
      />
    )

  // The breakdown is identical under both budgets, so either carries it.
  const shown = live ?? fit!
  // Whether the decode figure is genuinely a range. An older gateway sends no
  // empty-cache end at all, and a model whose cache costs nothing to read has
  // the two ends equal -- neither is a range, and rendering "24 to 24" would
  // invent a spread that is not there.
  const measured = result.measured_decode ?? null
  const hasRange =
    shown.predicted_decode_tps != null &&
    shown.predicted_decode_tps_empty != null &&
    shown.predicted_decode_tps_empty > shown.predicted_decode_tps
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

      {/* CUDA graph capture: enforce_eager trades startup time for decode
          throughput; cudagraph_capture_sizes does NOT trade startup time for
          anything -- capture wall clock tracks model size, not list length
          (measured: Qwen3-1.7B took ~30-45s whether it captured 3 sizes or
          94; pythia-70m took ~1s whether it captured 20 or 94), because the
          dominant cost is a roughly fixed per-launch warmup rather than a
          per-size one. What a shorter list buys is peak capture MEMORY,
          which does scale with length (0.64 GiB for ~94 graphs on
          Qwen3-1.7B against 0.01 GiB for 10) -- the same reason vLLM caps
          its own default list rather than leaving it unbounded. Neither is
          a modifier on the number above (that's speculativeSection's job).
          Basic mode only — a custom command replaces every plan-derived
          flag, so neither lever here would survive it. */}
      {!customCommand ? (
        <div style={{ display: 'grid', gap: 6, margin: '10px 0' }}>
          <label className="label" style={{ display: 'flex', alignItems: 'center', gap: 8, fontWeight: 400 }}>
            <input
              type="checkbox"
              checked={enforceEager}
              onChange={(e) => onEnforceEagerChange(e.target.checked)}
            />
            Force eager execution
          </label>
          <p className="unit" style={{ margin: 0 }}>
            Skips CUDA graph capture and torch.compile — the single slowest
            launch phase — at the cost of slower decode.
          </p>
          {!enforceEager ? (
            <>
              <input
                id="verdict-cudagraph-sizes"
                className="mono"
                type="text"
                placeholder={
                  concurrency
                    ? `e.g. ${cudagraphSizeHint(concurrency)} — holds less memory during capture, not a faster launch`
                    : '1 2 4 8 16 32 (trims capture memory, not launch time — leave blank for the runtime’s own default)'
                }
                value={cudagraphSizesText}
                onChange={(e) => onCudagraphSizesTextChange(e.target.value)}
              />
              <p className="unit" style={{ margin: 0 }}>
                Capture graphs for only these batch sizes instead of the
                runtime's own default list. This does not shorten the
                launch: capturing dozens of sizes costs about the same wall
                clock as capturing one, since the dominant cost is a
                fixed-ish per-launch warmup rather than a per-size one — for
                enforce_eager's kind of savings, tick the box above instead.
                What a shorter list buys is memory: each captured size holds
                its own replay buffers, so fewer sizes means less held
                during capture — real on a tight-memory launch, and the
                reason vLLM caps its own default list rather than leaving it
                unbounded.
                {concurrency
                  ? ` Nothing above ${concurrency} sequence` +
                    `${concurrency === 1 ? '' : 's'} is ever dispatched for ` +
                    `decode here, so trimming to it loses nothing there — `
                  : ' Nothing above this deployment’s own concurrency is ever dispatched for decode, so trimming to it loses nothing there — '}
                the one exception is a single prompt wide enough to fill one
                step past whatever cap is set, which falls back to eager for
                that step only.
              </p>
            </>
          ) : null}

          {/* KV cache width. In the same basic-mode-only block as the two
              above because a custom command replaces every plan-derived
              flag, but it is NOT the same kind of knob: those two change
              only what launches, while this changes what the gate APPROVES.
              The panel re-plans when it moves, so every number in this card
              is already the number for the chosen width -- which is the
              whole point. fp8 halves bytes-per-token, so it buys context, or
              throughput at a context already chosen, and it costs some
              accuracy on the cached keys and values. */}
          <label className="label" htmlFor="verdict-kv-dtype" style={{ fontWeight: 400 }}>
            KV cache width
          </label>
          <select
            id="verdict-kv-dtype"
            value={kvDtype}
            onChange={(e) => onKvDtypeChange(e.target.value)}
          >
            <option value="">The model's own (default)</option>
            <option value="fp8">fp8 — half the bytes per token</option>
            <option value="fp8_e4m3">fp8_e4m3</option>
            <option value="fp8_e5m2">fp8_e5m2</option>
          </select>
          <p className="unit" style={{ margin: 0 }}>
            Stores each cached key and value in one byte instead of two. The
            verdict above is recomputed for it, so the context and the memory
            figures shown are already the ones for this width — and the
            engine is told the same width the gate sized with, which is the
            only way those two numbers can be the same number.
          </p>

          {/* Weight quantization. The heavier twin of the control above:
              that one decides how wide a CACHE entry is, this one decides
              how wide a WEIGHT is, and bytes-per-parameter differs by 3.5x
              between bf16 and nvfp4. Same contract, therefore — the panel
              re-plans when it moves, so every figure in this card is already
              the figure for the chosen scheme, and the engine is told the
              same scheme the gate priced.

              Only the schemes a runtime can actually be told to load. The
              resolver prices 33 and most of them are llama.cpp's GGUF
              formats, which nothing here launches at all; offering those
              would be offering a launch that cannot start. nvfp4 is
              Blackwell-only and mxfp4 is emulated below it, which the
              coordinator refuses on the node rather than this list
              pretending to know the hardware. */}
          <label className="label" htmlFor="verdict-quant" style={{ fontWeight: 400 }}>
            Weight quantization
          </label>
          <select
            id="verdict-quant"
            value={forcedQuant}
            onChange={(e) => onForcedQuantChange(e.target.value)}
          >
            <option value="">As the checkpoint ships (default)</option>
            <option value="fp8">fp8 — 8 bits per weight</option>
            <option value="nvfp4">nvfp4 — 4.5 bits, Blackwell only</option>
            <option value="mxfp4">mxfp4 — 4.25 bits</option>
            <option value="awq_int4">awq_int4 — 4.5 bits</option>
            <option value="gptq_int4">gptq_int4 — 4.5 bits</option>
          </select>
          <p className="unit" style={{ margin: 0 }}>
            Forces the loader rather than letting it read the packing off the
            checkpoint. The weights the verdict above charges are already
            priced at this scheme, and the launch is told the same one — a
            runtime that cannot be told refuses the launch rather than
            quietly loading something else into a budget sized for this.
          </p>
        </div>
      ) : null}

      {/* The quantization picker, as a labeled subsection of this same card
          rather than a second `.verdict` box -- it is a different question
          (which repository, not whether this one fits) answered from a
          different call, so it keeps its own status line and Serve button
          rather than being reconciled into the verdict below it. Above the
          single-node launch info rather than below it: picking a
          quantization is the first decision, what it costs on this hardware
          is the second. */}
      {quantSection ? (
        <div style={{ marginBottom: 10, paddingBottom: 10, borderBottom: '1px solid var(--rule)' }}>
          <div className="unit" style={{ marginBottom: 6 }}>
            quantization
          </div>
          {quantSection}
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

      <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8, marginTop: 8 }}>
        <span className="unit">
          {gbytes(shown.breakdown.total, 1)} GB total
        </span>
        <span className="unit" style={{ color: shown.headroom < 0 ? 'var(--fault)' : undefined }}>
          {gbytes(shown.headroom, 1)} GB headroom{live ? ' right now' : ''}
        </span>
      </div>
      <Disclosure
        summary={breakdownOpen ? 'Hide the memory breakdown' : 'Show the memory breakdown'}
        open={breakdownOpen}
        onToggle={() => setBreakdownOpen((v) => !v)}
      >
        <dl
          style={{
            margin: 0,
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
      </Disclosure>

      {/* One condition, computed once: an older gateway sends no empty-cache
       *  figure at all, and a model whose cache costs nothing to read has the
       *  two ends equal -- neither is a range, and rendering "24 to 24" would
       *  invent a spread that is not there. */}
      {shown.predicted_decode_tps != null ? (
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginTop: 8 }}>
          <span className="unit">predicted decode</span>
          {/* A range, because decode reads the cache for the tokens actually
              present: a fresh request is genuinely faster than one that has
              filled the context, and on measured hardware the gap is roughly
              2x. Showing only the full-context end labelled it "the rate",
              which is the pessimistic end of a spread the user never saw.
              The upper end is the same figure the head scan uses as its own
              baseline, so the two now agree rather than quietly differing.

              Falls back to the single figure when the gateway predates the
              field -- an older coordinator must not render a broken range. */}
          {hasRange ? (
            <>
              {/* Low end first, matching the `speculating` row directly below,
                  which renders floor-then-ceiling. Two ranges on one card
                  reading in opposite directions is a card nobody can scan. The
                  low end is also the honest one to lead with: it is the rate
                  once the context is full, and it is the figure the degraded
                  threshold still judges. */}
              <Readout
                value={shown.predicted_decode_tps}
                decimals={1}
                width={5}
                unit=""
                tone={shown.verdict === 'fits_degraded' ? 'warn' : 'ink'}
              />
              <span className="unit">to</span>
            </>
          ) : null}
          <Readout
            value={hasRange ? shown.predicted_decode_tps_empty! : shown.predicted_decode_tps}
            decimals={1}
            width={5}
            unit="tok/s"
            tone={shown.verdict === 'fits_degraded' ? 'warn' : 'ink'}
          />
          {/* The fit gate's own figure is single stream -- `predict_decode_tps`
              says so, and the cache it is handed is one sequence's whatever
              concurrency the plan was sized for. Unlabelled, it reads as the
              machine's throughput, which it is not and which nothing here
              measures. */}
          <span className="unit">per sequence</span>
        </div>
      ) : null}

      {/* What a machine here actually did, when one has. Under the range and
          never instead of it: the range is what is true for a context nobody
          has run, and this is one measurement of one workload. The band is
          named because decode reads the cache for the tokens actually present,
          so a rate taken at 300 tokens says little about 8192. */}
      {measured != null ? (
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginTop: 4 }}>
          <span className="unit">measured here</span>
          <Readout value={measured.decode_tps} decimals={1} width={5} unit="tok/s" />
          <span className="unit">
            {`over ${measured.requests.toFixed(0)} request${
              measured.requests === 1 ? '' : 's'
            } near ${measured.context_band} tokens`}
          </span>
        </div>
      ) : null}

      {speculativeSection}

      {/* What speculative decoding would do to that figure, as a range and
       *  never a single number. Both ends are the fit gate's arithmetic and
       *  the sentence under them is the fit gate's own -- including the part
       *  that says derate does not measure acceptance rate, which is the
       *  clause that keeps this a statement of mechanism rather than a
       *  throughput claim. Rendered through `Verbatim` for exactly that
       *  reason: it is a planner/fit string, and those are the product.
       *
       *  The floor is deliberately shown even though it can be BELOW the
       *  ordinary rate above it. A method that drafts with real weights and
       *  gets nothing accepted is slower than not speculating at all, and a
       *  recommendation that showed only the ceiling would be selling the
       *  good half of a trade. */}
      {shown.speculative_decode_tps_ceiling != null &&
      shown.speculative_decode_tps_floor != null ? (
        <div style={{ display: 'grid', gap: 4, marginTop: 8 }}>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
            <span className="unit">speculating</span>
            <Readout
              value={shown.speculative_decode_tps_floor}
              decimals={1}
              width={5}
              unit=""
              tone={
                shown.predicted_decode_tps != null &&
                shown.speculative_decode_tps_floor < shown.predicted_decode_tps
                  ? 'warn'
                  : 'ink'
              }
            />
            <span className="unit">to</span>
            <Readout
              value={shown.speculative_decode_tps_ceiling}
              decimals={1}
              width={5}
              unit="tok/s"
            />
            {/* Same scope as the rate above, and it matters more here: a
                speculative ceiling is a low-concurrency claim, and the server's
                own sentence below says so whenever the plan is sized for a
                batch. */}
            <span className="unit">per sequence</span>
          </div>
          {shown.speculative_reason ? (
            <Verbatim text={shown.speculative_reason} size="label" />
          ) : null}
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
function NoVerdict({
  result,
  quantSection,
  speculativeSection,
}: {
  result: PlanResponse
  quantSection?: ReactNode
  speculativeSection?: ReactNode
}) {
  const p = result.plan
  return (
    <div className="verdict on bad">
      {quantSection ? (
        <div style={{ marginBottom: 10, paddingBottom: 10, borderBottom: '1px solid var(--rule)' }}>
          <div className="unit" style={{ marginBottom: 6 }}>
            quantization
          </div>
          {quantSection}
        </div>
      ) : null}
      <div className="vhead">
        <Lamp signal="fault" label="no verdict" /> {planShortFromDegrees(p)}
      </div>
      <Verbatim text={p.reason} size="label" />
      <p className="label" style={{ fontWeight: 400, margin: '10px 0 0' }}>
        The fit gate did not answer, so nothing here says whether this fits.
        Serve is withheld.
      </p>
      {/* Still shown with no verdict. What a checkpoint declares is the
          resolver's answer, not the fit gate's, so it is knowable here — and a
          control that appeared and vanished with the fit port's availability
          would read as a feature that comes and goes. */}
      {speculativeSection}
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
