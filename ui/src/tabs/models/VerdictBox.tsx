import type { FitResult, PullAccepted } from '../../api/types'
import { Icon } from '../../components/Icon'
import { Lamp } from '../../components/Lamp'
import { OverrideGate } from '../../components/OverrideGate'
import { Verbatim } from '../../components/Verbatim'
import { useRouter } from '../../state/router'
import { memoryPool, servesOnCluster } from '../../state/runtime'
import { gbytes } from '../../format'
import { claimFor } from './gates'
import { NodeBars } from './NodeBars'
import { partitionForRuntime } from './ollamaTarget'
import { isUnslothDynamic } from './quantPicks'
import { TERMINAL } from './rows'
import type { ModelSession } from './useModelSession'
import { gib, planWord } from './verdictView'

/** One decimal below ten, none above: "4.2" and "41", never "41.3". */
function rate(n: number): string {
  return n < 10 ? n.toFixed(1) : n.toFixed(0)
}

/** Degrees above one, in the planner's own abbreviations. */
function degreeText(p: {
  tensor_parallel: number
  pipeline_parallel: number
  expert_parallel: number
  data_parallel?: number
}): string {
  return [
    p.tensor_parallel > 1 ? `TP ${p.tensor_parallel}` : null,
    p.pipeline_parallel > 1 ? `PP ${p.pipeline_parallel}` : null,
    p.expert_parallel > 1 ? `EP ${p.expert_parallel}` : null,
    (p.data_parallel ?? 1) > 1 ? `DP ${p.data_parallel}` : null,
  ]
    .filter(Boolean)
    .join(' · ')
}

/** Which running deployments the "stop X" fixes offer first. */
const STOP_ORDER: Record<string, number> = { low: 0, normal: 1, high: 2, pinned: 3 }
const priorityOf = (d: { lifecycle?: { priority?: string } | null }) => d.lifecycle?.priority ?? 'normal'

/** The verdict, in one line, then the fit gate's own sentence under it.
 *
 *  Nothing here computes a fit. The word is `planWord` over the plan reply,
 *  the figure is the breakdown the gate sent, and every sentence the server
 *  wrote -- the reason, the refusal, the warnings, the gates -- renders
 *  through `Verbatim`. What the box adds is order: the one word somebody came
 *  for first, the button beside it, and the arguments underneath. */
/** A speculative method as the verdict line names it. */
const METHOD_WORD: Record<string, string> = { mtp: 'MTP', eagle: 'EAGLE', eagle3: 'EAGLE3', ngram: 'n-gram', dspark: 'DSpark' }

export function VerdictBox({ s, onAdvancedTab }: { s: ModelSession; onAdvancedTab?: () => void }) {
  const { navigate } = useRouter()
  const onCluster = servesOnCluster(s.runtime)
  const { result, variant, running } = s
  const fit = result?.fit ?? null
  const live = result?.fit_live ?? null
  const shown: FitResult | null = live ?? fit
  // Whether the picked runtime loads this file at all -- the same split the
  // quantization list draws, so the button and the list cannot disagree.
  const loadable = variant ? partitionForRuntime([variant], s.runtime, s.formats).servable.length > 0 : true
  const word = planWord({
    running: running != null,
    routing: s.routing != null,
    providerRuntime: !onCluster,
    variant,
    result,
    checking: s.checking,
    hostPool: memoryPool(s.runtime) === 'host',
  })

  // Where, in words. A running deployment is a fact and wins over any plan.
  let where = ''
  if (s.routing) where = `through ${s.routing.display_name}`
  else if (running)
    where = `on ${running.node_ids.join(', ')}${running.model_id !== s.subjectId ? ` · ${running.model_id}` : ''}`
  else if (!onCluster) where = s.provider ? `onto ${s.provider.display_name}` : ''
  else if (result) {
    const ids = result.plan.node_ids
    const degrees = degreeText(result.plan)
    where =
      ids.length > 1
        ? `across ${ids.length} nodes${degrees ? ` (${degrees})` : ''}`
        : ids[0]
          ? `on ${ids[0]}`
          : ''
  }
  // While it runs, the box is about the deployment, not the next launch.
  if (variant && !s.routing && !running) {
    where += `${where ? ' · ' : ''}${variant.label}${isUnslothDynamic(variant) ? ' (Unsloth Dynamic)' : ''}`
  }
  const specMethod = !running && !s.routing ? (result?.speculative?.method ?? null) : null
  if (specMethod) where += `${where ? ' · ' : ''}${METHOD_WORD[specMethod] ?? specMethod}`

  // A running deployment carries the fit it was launched with; a routed model
  // has no memory here to state. Neither gets a placeholder dash.
  const runningBytes = running?.fit?.breakdown?.total ?? null
  const figure = s.routing
    ? ''
    : running
      ? runningBytes != null
        ? `${gib(runningBytes)} per node`
        : ''
      : shown
        ? `${gib(shown.breakdown.total)} per node`
        : ''

  const cached = variant
    ? s.cache.complete(variant.repo_id, variant.file_bytes) === true
    : s.cache.nodes(s.subjectId).length > 0

  let button: JSX.Element | null = null
  if (s.routing) {
    button = null
  } else if (running) {
    button = (
      <button type="button" className="mdl-primary" onClick={() => navigate({ dest: 'chat' })}>
        Open in Chat
      </button>
    )
  } else if (!onCluster) {
    button =
      s.canServe && s.provider ? (
        <button type="button" className="mdl-primary" disabled={s.launching} onClick={() => void s.serve()}>
          {s.launching ? 'Pulling…' : `Pull onto ${s.provider.display_name}`}
        </button>
      ) : null
  } else if (s.canServe && loadable) {
    button = (
      <button type="button" className="mdl-primary" disabled={s.launching} onClick={() => void s.serve()}>
        {s.launching
          ? 'Launching…'
          : s.gates.length > 0
            ? 'Serve anyway'
            : cached
              ? 'Serve'
              : 'Download and serve'}
      </button>
    )
  }

  // The gate said what to change; these are those changes, one click each.
  // Prefer the live figure: the most context that fits on an idle machine is
  // not an offer anybody can act on right now.
  const maxCtx = shown?.max_context_that_fits ?? null
  const fixes: { key: string; label: string; act: () => void }[] = []
  if (!running && result && maxCtx != null && maxCtx > 0 && result.context != null && maxCtx < result.context) {
    fixes.push({
      key: 'ctx',
      label: `use ${maxCtx.toLocaleString()} context`,
      act: () => navigate({ context: maxCtx }, { replace: true }),
    })
  }
  if (word.label === 'Not now' && result) {
    const planned = new Set(result.plan.node_ids)
    // Ordered by the operator's own priority, least important first, and a
    // pinned deployment is never offered: priority exists for this list.
    const candidates = (s.cluster?.deployments ?? [])
      .filter((d) => !TERMINAL.has(d.state) && d.node_ids.some((n) => planned.has(n)))
      .filter((d) => priorityOf(d) !== 'pinned')
      .sort((a, b) => (STOP_ORDER[priorityOf(a)] ?? 1) - (STOP_ORDER[priorityOf(b)] ?? 1))
    for (const d of candidates) {
      fixes.push({
        key: d.deployment_id,
        label: `stop ${d.served_name}`,
        act: () => {
          if (window.confirm(`Stop ${d.served_name}? Requests to it fail until it is served again.`)) {
            void s.stop(d.deployment_id)
          }
        },
      })
    }
  }
  if (!running && !s.routing && onCluster && onAdvancedTab && shown && shown.verdict !== 'fits') {
    fixes.push({ key: 'advanced', label: 'open Advanced', act: onAdvancedTab })
  }

  // What the reader should not miss, each in the words of whoever said it.
  const caveats: string[] = []
  const push = (line: string | null | undefined) => {
    if (line && !caveats.includes(line)) caveats.push(line)
  }
  // A running model's box is about the deployment; a plan for another launch
  // would put warnings about weights nobody is loading beside "Running".
  if (result && onCluster && !s.routing && !running) {
    if (result.degrees?.rejection) push(result.degrees.rejection)
    for (const w of fit?.warnings ?? []) push(w)
    for (const w of live?.warnings ?? []) push(w)
    for (const w of result.resolver_warnings) push(w)
    for (const w of result.placement?.warnings ?? []) push(w)
    if (!live && result.serve?.unavailable_reason) {
      push(
        `No live memory reading: ${result.serve.unavailable_reason}. Only the idle-hardware answer is available.`,
      )
    }
    if (!fit && !live) {
      push('The fit gate did not answer, so nothing here says whether this fits. Serve is withheld.')
    }
    for (const [a, b] of s.board.unmeasuredPairs) {
      push(
        `The link between ${a} and ${b} has never been measured, so the planner falls back to a conservative split. Measure it under Advanced › Parallelism.`,
      )
    }
  }
  // Client-written, not a gate string: it renders as plain text below.
  const orig = result?.original_window ?? null
  const yarnNote =
    result && !running && !s.routing && orig != null && result.context != null && result.context > orig && !s.engineOptions.rope_factor
      ? `${Math.round(result.context / 1024)}k is past this model's trained ${Math.round(orig / 1024)}k window. Turn on YaRN under Advanced › Context, or answers degrade past ${Math.round(orig / 1024)}k.`
      : null
  if (!s.routing && onCluster && !loadable) {
    push(
      `This file can’t be loaded by ${s.runtime}. Pick another quant, or choose a provider under Advanced › Engine.`,
    )
  }

  // A refusal with nothing on offer is the reason itself, said once.
  const refusal =
    result?.serve && !result.serve.allowed && s.gates.length === 0 && result.serve.reason !== shown?.reason
      ? result.serve.reason
      : null

  const full = shown?.predicted_decode_tps ?? null
  const empty = shown?.predicted_decode_tps_empty ?? null
  const decode =
    full != null && empty != null && empty > full
      ? `Decode ${rate(full)}–${rate(empty)} tok/s per sequence`
      : full != null || empty != null
        ? `Decode ${rate((full ?? empty)!)} tok/s per sequence`
        : null
  const specFloor = shown?.speculative_decode_tps_floor ?? null
  const specCeiling = shown?.speculative_decode_tps_ceiling ?? null
  const measured = result?.measured_decode ?? null

  return (
    <section className="mdl-verdict" aria-label="Verdict" aria-busy={s.checking}>
      <div className="mdl-verdict-row">
        <Lamp signal={word.signal} hollow={word.signal === 'idle'} size={10} label={word.label} />
        <span className={`mdl-word mdl-sig-${word.signal}`}>{word.label}</span>
        <span className="mdl-where">{where}</span>
        {figure ? <span className="mdl-figure mono">{figure}</span> : null}
        {button}
      </div>

      {shown && onCluster && !s.routing && !running ? (
        <div className="mdl-say">
          <Verbatim text={shown.reason} size="label" />
          {decode ? <span className="unit">{decode}</span> : null}
          {specFloor != null && specCeiling != null ? (
            <>
              <span className="unit">
                {`Speculating ${rate(specFloor)}–${rate(specCeiling)} tok/s per sequence`}
              </span>
              {shown.speculative_reason ? <Verbatim text={shown.speculative_reason} size="unit" /> : null}
            </>
          ) : null}
          {measured ? (
            <span className="unit">
              {`Measured ${rate(measured.decode_tps)} tok/s per sequence at ${measured.context_band} context`}
            </span>
          ) : null}
        </div>
      ) : null}

      {!onCluster && !s.routing && variant ? (
        <p className="unit mdl-say">
          {`${gib(variant.file_bytes)} to download. Nothing here is judged by the fit gate: these weights would run on ${
            s.provider ? s.provider.display_name : 'a provider'
          }, whose memory derate does not manage — the pull is what weighs them, against that machine's own free memory.`}
        </p>
      ) : null}

      {fixes.length ? (
        <p className="mdl-fixes label">
          {fixes.map((f, i) => (
            <span key={f.key}>
              {i === 0 ? 'or ' : ' or '}
              <button type="button" className="mdl-textbtn" onClick={f.act}>
                {f.label}
              </button>
            </span>
          ))}
        </p>
      ) : null}

      {refusal ? (
        <div className="mdl-refusal">
          <Verbatim text={refusal} size="label" />
        </div>
      ) : null}

      {s.gates.length > 0 && !running ? (
        <div className="mdl-gates">
          {s.gates.map((g) => (
            <OverrideGate
              key={g.param}
              // Said once: when the gate's refusal is the very sentence the
              // verdict above already printed, the gate only adds its box.
              reason={g.reason === shown?.reason ? null : g.reason}
              sentence={claimFor(g, live, fit?.usable_per_node ?? null, result?.plan ?? { node_ids: [] })}
              checked={s.granted[g.param] === true}
              onChange={(v) => s.grant(g.param, v)}
            />
          ))}
          {/* With more than one gate the count says how far along you are,
              because otherwise ticking the first box appears to do nothing. */}
          {s.gates.length > 1 ? (
            <span className="unit">
              {s.gates.filter((g) => s.granted[g.param] === true).length} of {s.gates.length} agreed
            </span>
          ) : null}
        </div>
      ) : null}

      {caveats.length ? (
        <ul className="mdl-caveats">
          {caveats.map((c) => (
            <li key={c}>
              <span className="mdl-caveat-icon">
                <Icon name="alert-triangle" size={14} label="Warning" />
              </span>
              <Verbatim text={c} size="unit" />
            </li>
          ))}
        </ul>
      ) : null}
      {yarnNote ? <p className="unit mdl-say">{yarnNote}</p> : null}

      {s.error ? <p className="mdl-error label">{s.error}</p> : null}

      {s.pulled && !onCluster ? <Pulled reply={s.pulled} /> : null}

      {onCluster && !s.routing ? <NodeBars s={s} /> : null}
    </section>
  )
}

/** What the pull endpoint accepted, said in full -- including whether a gate
 *  ran. `free_bytes` and `budget_bytes` are both 0 for a machine that never
 *  joined the cluster AND for one measured with nothing free; `gated` is what
 *  separates them, and without saying it the screen would report "0 GiB free"
 *  about a box nobody looked at. */
function Pulled({ reply }: { reply: PullAccepted }) {
  if (!reply.download_bytes) {
    return <p className="unit">already on {reply.checked_against}</p>
  }
  return (
    <p className="unit mdl-pulled">
      <Lamp signal="warn" label={`pulling ${reply.model}`} />
      <span>
        pulling {gbytes(reply.download_bytes, 2)} GiB
        {reply.gated
          ? ` — ${gbytes(reply.free_bytes, 2)} GiB free on ${reply.checked_against}`
          : ` — ${reply.checked_against} has not joined the cluster, so nothing weighed this`}
        . Progress is under Downloads. Restarting the coordinator cancels it.
      </span>
      {/* The name it will answer to, which is NOT this repository's id: Ollama
          reports a pulled model under the ref it was given, so it arrives in
          /v1/models as `hf.co/<repo>:<tag>` and appears as its own row. Not
          aliased back, because an alias goes through the key screen whose
          entropy heuristic refuses ordinary GGUF repository names. */}
      <span className="mono">served as {reply.model}</span>
    </p>
  )
}
