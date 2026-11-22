import { useId, useState } from 'react'
import { Disclosure } from '../../components/Panel'
import { Select, type SelectOption } from '../../components/Select'
import { Verbatim } from '../../components/Verbatim'
import { useBackend } from '../../state/backend'
import { useRuntimes } from '../../state/resources'
import { useRouter } from '../../state/router'
import { RUNTIME_OPTIONS, servesOnCluster, shardsAcrossNodes, type Runtime } from '../../state/runtime'
import { DegreeFields } from './DegreeFields'
import { LinkState, ParallelismPicker } from './ParallelismPicker'
import { ProviderPicker } from './ProviderPicker'
import { SpeculativeField } from './SpeculativeField'
import type { ModelSession } from './useModelSession'
import llamacppLogo from '../../../../.omp/design/logos/llamacpp.png'
import ollamaLogo from '../../../../.omp/design/logos/ollama.svg'
import sglangLogo from '../../../../.omp/design/logos/sglang.png'
import vllmLogo from '../../../../.omp/design/logos/vllm.svg'
import {
  ApiFields,
  CommandPreviewBlock,
  DrafterSearch,
  EnvRows,
  FlagHints,
  LifecycleFields,
  MultimodalFields,
  PrefixAndShare,
  ProfileBar,
  RuntimeFacts,
  SchedulerFields,
  YarnField,
} from './AdvancedSections'

const TARGET_OPTIONS: SelectOption<'throughput' | 'latency'>[] = [
  { value: 'throughput', label: 'throughput' },
  { value: 'latency', label: 'latency' },
]

/** What each engine is for, said once. Fixed copy, not a verdict: whether one
 *  can serve THIS model is the verdict box's answer after you pick it.
 *  `logo` is the project's own mark from `.omp/design/logos/`; the TTS server
 *  is derate's own and has none, so it keeps its initials. */
const ENGINE: Record<Runtime, { tile: string; logo?: string; note: string }> = {
  vllm: { tile: 'vL', logo: vllmLogo, note: 'The default. Widest model and speculative-decoding support.' },
  sglang: {
    tile: 'SG',
    logo: sglangLogo,
    note: 'Radix prefix cache; strong when many requests share a long prompt. No speculative decoding here.',
  },
  tts: { tile: 'TT', note: 'derate’s own speech server, one checkpoint, one process.' },
  llamacpp: { tile: 'LC', logo: llamacppLogo, note: 'GGUF on a machine with no GPU.' },
  ollama: { tile: 'OL', logo: ollamaLogo, note: 'Pull a GGUF onto a provider machine; the gateway routes to it.' },
}

/** The KV cache widths the gate prices AND the pinned engine accepts. */
const KV_OPTIONS: { value: string; label: string }[] = [
  { value: '', label: 'The model’s own (default)' },
  { value: 'fp8', label: 'fp8 — half the bytes per token' },
  { value: 'fp8_e4m3', label: 'fp8_e4m3' },
  { value: 'fp8_e5m2', label: 'fp8_e5m2' },
]

/** Only the schemes a runtime can actually be told to load. The resolver
 *  prices 33 and most are llama.cpp's GGUF formats, which nothing here can be
 *  told to load into a safetensors checkpoint. nvfp4 is Blackwell-only and
 *  mxfp4 is emulated below it, which the coordinator refuses on the node
 *  rather than this list pretending to know the hardware. */
const QUANT_OPTIONS: SelectOption<string>[] = [
  { value: '', label: 'As the checkpoint ships (default)' },
  { value: 'fp8', label: 'fp8 — 8 bits per weight' },
  { value: 'nvfp4', label: 'nvfp4 — 4.5 bits, Blackwell only' },
  { value: 'mxfp4', label: 'mxfp4 — 4.25 bits' },
  { value: 'awq_int4', label: 'awq_int4 — 4.5 bits' },
  { value: 'gptq_int4', label: 'gptq_int4 — 4.5 bits' },
]

const CONTEXT_CHIPS: { label: string; value: number }[] = [
  { label: '8k', value: 8192 },
  { label: '32k', value: 32768 },
  { label: '128k', value: 131072 },
]

type Group = 'engine' | 'speed' | 'memory' | 'context' | 'parallel' | 'multimodal' | 'api' | 'lifecycle' | 'flags'

/** An example capture list that never suggests a size above `concurrency`
 *  itself -- doubling from 1 up to it, then the exact value. Illustrative
 *  only: never parsed, never sent, just what the placeholder shows. */
function cudagraphSizeHint(concurrency: number): string {
  const sizes: number[] = []
  for (let n = 1; n < concurrency; n *= 2) sizes.push(n)
  sizes.push(concurrency)
  return sizes.join(' ')
}

/** Every knob the launch takes, grouped, each summary saying where it is set.
 *
 *  Nothing here is a second copy of state: `useModelSession` owns every value
 *  and both the plan and the launch read them from there, so a control moved
 *  here re-plans the verdict box the same way it always did. */
export function AdvancedTab({ s }: { s: ModelSession }) {
  const { route, navigate } = useRouter()
  const { backend, invalidate } = useBackend()
  const uid = useId()
  const [open, setOpen] = useState<Partial<Record<Group, boolean>>>({ engine: true })
  const flip = (g: Group) => setOpen((o) => ({ ...o, [g]: !o[g] }))
  const [measuring, setMeasuring] = useState<Record<string, boolean>>({})
  const [measureError, setMeasureError] = useState<Record<string, string>>({})

  const onCluster = servesOnCluster(s.runtime)
  const overridden = route.context !== null || route.concurrency !== null
  const result = s.result
  const runtimes = useRuntimes().data ?? null
  const info = runtimes?.find((r) => r.name === s.runtime) ?? null
  // The variant's own format, which a runtime either loads or does not.
  const format = s.variant?.gguf_file ? 'gguf' : 'safetensors'
  const kvOptions = info
    ? [{ value: '', label: 'The model’s own (default)' }, ...info.kv_dtypes.map((d) => ({ value: d, label: d }))]
    : KV_OPTIONS

  const measure = async (a: string, b: string) => {
    const key = `${a} ${b}`
    setMeasuring((m) => ({ ...m, [key]: true }))
    setMeasureError((m) => ({ ...m, [key]: '' }))
    try {
      await backend.measureLink(a, b)
      invalidate()
    } catch (e) {
      setMeasureError((m) => ({ ...m, [key]: e instanceof Error ? e.message : String(e) }))
    } finally {
      setMeasuring((m) => ({ ...m, [key]: false }))
    }
  }

  const ticked = s.board.rows.filter((r) => r.ticked)
  const worstLink = ticked.reduce<number | null>(
    (w, r) => (r.linkGbps == null ? w : w == null ? r.linkGbps : Math.min(w, r.linkGbps)),
    null,
  )
  const tickedIds = new Set(ticked.map((r) => r.nodeId))
  const seenPairs = new Set<string>()
  const plannedLinks = (s.cluster?.links ?? []).filter((l) => {
    const pair = [l.src, l.dst].sort().join(' ')
    if (!tickedIds.has(l.src) || !tickedIds.has(l.dst) || seenPairs.has(pair)) return false
    seenPairs.add(pair)
    return true
  })

  return (
    <div className="mdl-adv">
      <ProfileBar s={s} />
      <Disclosure summary={`Engine — ${s.runtime}`} open={open.engine === true} onToggle={() => flip('engine')}>
        <div className="mdl-engines" role="radiogroup" aria-label="Engine">
          {RUNTIME_OPTIONS.map((o) => {
            const id = `${uid}-rt-${o.value}`
            const rt = runtimes?.find((r) => r.name === o.value) ?? null
            const wrongFormat = rt != null && !rt.formats.includes(format)
            return (
              <div key={o.value} className="mdl-engine">
                <label htmlFor={id} className="mdl-engine-row">
                  <input
                    id={id}
                    type="radio"
                    name={`${uid}-runtime`}
                    checked={s.runtime === o.value}
                    disabled={wrongFormat && s.runtime !== o.value}
                    onChange={() => s.setRuntime(o.value)}
                  />
                  <span className="mdl-tile" aria-hidden="true">
                    {ENGINE[o.value].logo ? <img src={ENGINE[o.value].logo} alt="" /> : ENGINE[o.value].tile}
                  </span>
                  <span className="mdl-engine-text">
                    <span className="label">{rt?.display_name ?? o.label}</span>
                    <span className="unit">
                      {wrongFormat ? `Does not load ${format}; this variant is ${format}.` : ENGINE[o.value].note}
                    </span>
                    {rt ? <RuntimeFacts info={rt} /> : null}
                  </span>
                </label>
                {/* A control that moved on its own says what moved it. */}
                {s.runtime === o.value && s.runtimeBecause ? (
                  <div className="mdl-engine-because">
                    <Verbatim text={s.runtimeBecause} size="unit" />
                  </div>
                ) : null}
                {s.runtime === o.value && o.value === 'ollama' ? (
                  <div className="mdl-engine-because">
                    <ProviderPicker
                      targets={s.pullTargets}
                      chosen={s.providerId}
                      onChoose={s.setProviderId}
                      nodes={s.cluster?.nodes}
                    />
                  </div>
                ) : null}
              </div>
            )
          })}
        </div>
        <div className="fld mdl-fld-inline">
          <label htmlFor={`${uid}-target`}>Optimise for</label>
          <Select id={`${uid}-target`} value={s.target} options={TARGET_OPTIONS} onChange={s.setTarget} />
        </div>
      </Disclosure>

      {onCluster ? (
        <Disclosure
          summary={`Speed — ${route.spec ? `speculating (${route.spec.method})` : 'one token per step'}${s.enforceEager ? ', eager' : ''}`}
          open={open.speed === true}
          onToggle={() => flip('speed')}
        >
          {result ? (
            <SpeculativeField
              options={result.speculative_options ?? []}
              chosen={route.spec}
              onChoose={(spec) => navigate({ spec }, { replace: true })}
              modelId={s.subjectId}
            />
          ) : null}
          {!s.customCommand ? <DrafterSearch s={s} /> : null}
          {s.customCommand ? (
            <p className="unit">A custom command replaces every flag these would add, so they are off while it is in use.</p>
          ) : (
            <div className="mdl-stack">
              <SchedulerFields s={s} />
              <label className="mdl-check label">
                <input type="checkbox" checked={s.enforceEager} onChange={(e) => s.setEnforceEager(e.target.checked)} />
                Eager mode <span className="mono unit">--enforce-eager</span>
              </label>
              <p className="unit">
                Skips CUDA graph capture and torch.compile — the single slowest launch phase — at the cost of
                slower decode.
              </p>
              {!s.enforceEager ? (
                <div className="fld">
                  <label htmlFor={`${uid}-graphs`}>CUDA graph sizes</label>
                  <input
                    id={`${uid}-graphs`}
                    className="mono"
                    type="text"
                    placeholder={
                      result?.concurrency
                        ? `e.g. ${cudagraphSizeHint(result.concurrency)} — holds less memory during capture, not a faster launch`
                        : '1 2 4 8 16 32 (trims capture memory, not launch time — leave blank for the runtime’s own default)'
                    }
                    value={s.cudagraphSizesText}
                    onChange={(e) => s.setCudagraphSizesText(e.target.value)}
                  />
                  <p className="unit">
                    Capture graphs for only these batch sizes instead of the runtime's own default list. This does
                    not shorten the launch: capturing dozens of sizes costs about the same wall clock as capturing
                    one, since the dominant cost is a fixed-ish per-launch warmup rather than a per-size one — for
                    enforce_eager's kind of savings, tick the box above instead. What a shorter list buys is
                    memory: each captured size holds its own replay buffers, so fewer sizes means less held during
                    capture — real on a tight-memory launch, and the reason vLLM caps its own default list rather
                    than leaving it unbounded.
                    {result?.concurrency
                      ? ` Nothing above ${result.concurrency} sequence` +
                        `${result.concurrency === 1 ? '' : 's'} is ever dispatched for ` +
                        `decode here, so trimming to it loses nothing there — `
                      : ' Nothing above this deployment’s own concurrency is ever dispatched for decode, so trimming to it loses nothing there — '}
                    the one exception is a single prompt wide enough to fill one step past whatever cap is set,
                    which falls back to eager for that step only.
                  </p>
                </div>
              ) : null}
            </div>
          )}
        </Disclosure>
      ) : null}

      {onCluster ? (
        <Disclosure
          summary={`Memory — kv ${s.kvDtype || 'default'}${s.forcedQuant ? `, weights ${s.forcedQuant}` : ''}`}
          open={open.memory === true}
          onToggle={() => flip('memory')}
        >
          {s.customCommand ? (
            <p className="unit">A custom command replaces every flag these would add, so they are off while it is in use.</p>
          ) : (
            <div className="mdl-stack">
              {/* KV cache width. Unlike a launch-only flag this changes what
                  the gate APPROVES: the verdict re-plans when it moves, so
                  every number above is already the number for this width. */}
              <div className="mdl-kvgrid" role="radiogroup" aria-label="KV cache precision">
                {kvOptions.map((o) => {
                  const bytes = o.value ? info?.kv_dtype_bytes?.[o.value] : undefined
                  return (
                    <label key={o.value || 'default'} className="mdl-check label">
                      <input
                        type="radio"
                        name={`${uid}-kv`}
                        checked={s.kvDtype === o.value}
                        onChange={() => s.setKvDtype(o.value)}
                      />
                      <span>{o.label}</span>
                      <span className="mono unit">{bytes != null ? `×${bytes / 2}` : o.value ? '—' : ''}</span>
                      <span className="mono unit">{o.value ? `--kv-cache-dtype ${o.value}` : ''}</span>
                    </label>
                  )
                })}
              </div>
              <p className="unit">
                Stores each cached key and value in one byte instead of two. The verdict above is recomputed for
                it, so the context and the memory figures shown are already the ones for this width — and the
                engine is told the same width the gate sized with, which is the only way those two numbers can be
                the same number.
              </p>
              <PrefixAndShare s={s} />
              {s.forcedQuantApplies ? (
                <>
                  <div className="fld">
                    <label htmlFor={`${uid}-quant`}>Force weight quantization</label>
                    <Select
                      id={`${uid}-quant`}
                      value={s.forcedQuant}
                      options={QUANT_OPTIONS}
                      onChange={s.setForcedQuant}
                    />
                  </div>
                  <p className="unit">
                    Forces the loader rather than letting it read the packing off the checkpoint. The weights the
                    verdict above charges are already priced at this scheme, and the launch is told the same one —
                    a runtime that cannot be told refuses the launch rather than quietly loading something else
                    into a budget sized for this.
                  </p>
                </>
              ) : null}
            </div>
          )}
        </Disclosure>
      ) : null}

      {onCluster ? (
        <Disclosure
          summary={
            overridden
              ? `Context — you set ${s.liveContext.toLocaleString()} × ${s.liveConcurrency}`
              : 'Context — chosen for you'
          }
          open={open.context === true || (open.context === undefined && overridden)}
          onToggle={() => setOpen((o) => ({ ...o, context: !(o.context ?? overridden) }))}
        >
          <div className="mdl-ctx">
            <div className="fld mdl-numfld">
              <label htmlFor={`${uid}-ctx`}>Context</label>
              <input
                id={`${uid}-ctx`}
                className="mono"
                type="number"
                min={1}
                value={s.liveContext}
                onChange={(e) => {
                  const n = Number(e.target.value)
                  if (Number.isFinite(n) && n > 0) navigate({ context: Math.round(n) }, { replace: true })
                }}
              />
            </div>
            <div className="fld mdl-numfld">
              <label htmlFor={`${uid}-seq`}>Sequences</label>
              <input
                id={`${uid}-seq`}
                className="mono"
                type="number"
                min={1}
                value={s.liveConcurrency}
                onChange={(e) => {
                  const n = Number(e.target.value)
                  if (Number.isFinite(n) && n > 0) navigate({ concurrency: Math.round(n) }, { replace: true })
                }}
              />
            </div>
            <span className="chips mdl-chips-inline" role="group" aria-label="Context presets">
              {CONTEXT_CHIPS.map((c) => (
                <button
                  key={c.label}
                  type="button"
                  aria-pressed={route.context === c.value}
                  onClick={() => navigate({ context: c.value }, { replace: true })}
                >
                  {c.label}
                </button>
              ))}
            </span>
            {/* The way back. Without it an override is a one-way door: once a
                number is in the URL there is no value you can type that means
                "you choose", and clearing the box types 0. */}
            {overridden ? (
              <button
                type="button"
                className="ghost"
                onClick={() => navigate({ context: null, concurrency: null }, { replace: true })}
              >
                Choose for me
              </button>
            ) : null}
          </div>
          <p className="unit">
            {!overridden
              ? result?.context
                ? `Judged at ${result.context.toLocaleString()} context and ` +
                  `${result.concurrency ?? 1} ` +
                  `${(result.concurrency ?? 1) === 1 ? 'sequence' : 'sequences'} — ` +
                  'the largest window that fits here, capped at this model’s own.'
                : 'The coordinator picks the largest window that fits, capped at this model’s own.'
              : 'Every verdict on this screen is taken at these numbers.'}
          </p>
          {!s.customCommand ? <YarnField s={s} /> : null}
        </Disclosure>
      ) : null}

      {onCluster && shardsAcrossNodes(s.runtime) ? (
        <Disclosure
          summary={`Parallelism — ${s.degrees ? 'your degrees' : 'planner picks'}${
            s.board.unmeasuredPairs.length ? `, ${s.board.unmeasuredPairs.length} link unmeasured` : ''
          }`}
          open={open.parallel === true}
          onToggle={() => flip('parallel')}
        >
          <ParallelismPicker s={s} active={open.parallel === true} />
          <details className="mdl-subdetails">
            <summary className="unit">Set degrees by hand</summary>
            <div className="bararea">
              <DegreeFields
                idPrefix={`${uid}-dg`}
                degrees={s.degrees}
                onChange={s.setDegrees}
                effective={result?.plan ?? null}
                recommended={result?.recommended_plan ?? null}
                overruled={result?.degrees?.source === 'operator'}
              />
            </div>
          </details>
          {s.board.unmeasuredPairs.length ? (
            <div className="mdl-stack">
              {s.board.unmeasuredPairs.map(([a, b]) => {
                const key = `${a} ${b}`
                return (
                  <div key={key} className="mdl-link">
                    <span className="mono unit">{`${a} ↔ ${b}: never measured`}</span>
                    <button
                      type="button"
                      className="ghost"
                      disabled={measuring[key] === true}
                      onClick={() => void measure(a, b)}
                    >
                      {measuring[key] ? 'Measuring…' : 'Measure'}
                    </button>
                    {measureError[key] ? <Verbatim text={measureError[key]!} size="unit" /> : null}
                  </div>
                )
              })}
              <p className="unit">
                The planner reads bandwidth for the whole set or not at all, so one unmeasured pair plans this as
                an unmeasured cluster. Measuring saturates the link and can take several minutes on a pair that
                has never been calibrated.
              </p>
            </div>
          ) : ticked.length > 1 ? (
            <div className="mdl-stack">
              {plannedLinks.map((l) => {
                const key = `${l.src} ${l.dst}`
                return (
                  <LinkState
                    key={key}
                    link={l}
                    busy={measuring[key] === true}
                    error={measureError[key] ?? ''}
                    onMeasure={() => void measure(l.src, l.dst)}
                  />
                )
              })}
              {worstLink != null ? (
                <p className="unit">{`Link measured: ${worstLink.toFixed(1)} GB/s worst all-reduce`}</p>
              ) : null}
            </div>
          ) : null}
        </Disclosure>
      ) : null}

      {onCluster ? (
        <Disclosure summary="Multimodal" open={open.multimodal === true} onToggle={() => flip('multimodal')}>
          <MultimodalFields s={s} />
        </Disclosure>
      ) : null}

      {onCluster ? (
        <Disclosure
          summary={`API — ${s.servedName.trim() ? `served as ${s.servedName.trim()}` : 'name chosen for you'}`}
          open={open.api === true}
          onToggle={() => flip('api')}
        >
          <div className="mdl-stack">
            <div className="fld">
              <label htmlFor={`${uid}-served`}>Name clients use</label>
              <input
                id={`${uid}-served`}
                className="mono"
                type="text"
                spellCheck={false}
                placeholder="the coordinator picks one"
                value={s.servedName}
                onChange={(e) => s.setServedName(e.target.value)}
              />
            </div>
            {/* Opt-in, beside the warning that names what it would run: no
                launch executes a repository's own Python unless ticked. */}
            {result?.remote_code ? (
              <label className="mdl-check label">
                <input
                  type="checkbox"
                  checked={s.trustRemoteCode}
                  onChange={(e) => s.setTrustRemoteCode(e.target.checked)}
                />
                <span>
                  Trust remote code — run this repository’s own Python inside the model container
                  (--trust-remote-code). Leave it off unless you trust the repository named above. The launch is
                  not given your HuggingFace token unless the repository is gated.
                </span>
              </label>
            ) : null}
            {!s.customCommand ? <ApiFields s={s} info={info} /> : null}
          </div>
        </Disclosure>
      ) : null}

      {onCluster ? (
        <Disclosure
          summary={`Lifecycle — ${(s.running?.lifecycle ?? s.lifecycle)?.idle_unload_s ? 'unloads when idle' : 'stays loaded'}`}
          open={open.lifecycle === true}
          onToggle={() => flip('lifecycle')}
        >
          <LifecycleFields s={s} />
        </Disclosure>
      ) : null}

      {onCluster ? (
        <Disclosure
          summary={`Custom flags — ${
            s.customCommand
              ? 'whole command replaced'
              : s.flags.filter((f) => f.k.trim()).length
                ? `${s.flags.filter((f) => f.k.trim()).length} added`
                : 'none'
          }`}
          open={open.flags === true || (open.flags === undefined && s.customCommand)}
          onToggle={() => setOpen((o) => ({ ...o, flags: !(o.flags ?? s.customCommand) }))}
        >
          <div className="mdl-stack">
            <label className="mdl-check label">
              <input
                type="checkbox"
                checked={s.customCommand}
                onChange={(e) => s.setCustomCommand(e.target.checked)}
              />
              Replace the whole command
            </label>
            {s.customCommand ? (
              <>
                <textarea
                  className="mono mdl-cmd"
                  aria-label="Custom command"
                  spellCheck={false}
                  placeholder="--tensor-parallel-size 2 --gpu-memory-utilization 0.85 --max-model-len 32768"
                  value={s.customCommandText}
                  onChange={(e) => s.setCustomCommandText(e.target.value)}
                />
                <p className="unit">
                  Replaces the generated command entirely — the TP, PP, context, concurrency and memory figures
                  above become informational only; only what you type here is sent. --host, --port and
                  --served-model-name still get pinned to what the gateway needs after it, no matter what you
                  write. Each token is checked against a safety allowlist server-side; a rejected one is refused
                  with the reason named.
                </p>
              </>
            ) : (
              <>
                {s.flags.map((f, i) => (
                  <div key={i} className="mdl-flag">
                    <input
                      className="mono"
                      type="text"
                      aria-label={`Flag ${i + 1}`}
                      spellCheck={false}
                      placeholder="--flag-name"
                      value={f.k}
                      onChange={(e) => s.setFlags(s.flags.map((x, j) => (j === i ? { ...x, k: e.target.value } : x)))}
                    />
                    <input
                      className="mono"
                      type="text"
                      aria-label={`Value of flag ${i + 1}`}
                      spellCheck={false}
                      placeholder="value (blank for a switch)"
                      value={f.v}
                      onChange={(e) => s.setFlags(s.flags.map((x, j) => (j === i ? { ...x, v: e.target.value } : x)))}
                    />
                    <button
                      type="button"
                      className="ghost"
                      onClick={() => s.setFlags(s.flags.filter((_, j) => j !== i))}
                    >
                      Remove
                    </button>
                  </div>
                ))}
                <div>
                  <button type="button" className="ghost" onClick={() => s.setFlags([...s.flags, { k: '', v: '' }])}>
                    Add flag
                  </button>
                </div>
                <FlagHints s={s} />
                <p className="unit">Each flag is checked by the coordinator; a rejected one comes back naming the token.</p>
                <EnvRows s={s} tunables={info?.nccl_tunables ?? []} />
              </>
            )}
          </div>
        </Disclosure>
      ) : null}

      {onCluster ? <CommandPreviewBlock s={s} /> : null}
    </div>
  )
}
