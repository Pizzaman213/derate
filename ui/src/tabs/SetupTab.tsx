import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useBackend } from '../state/backend'
import { useRouter } from '../state/router'
import {
  LAUNCH_PHASES,
  LAUNCH_PHASE_LABEL,
  type LaunchPhase,
} from '../state/launchPhase'
import { Qr } from './setup/Qr'
import './setup/setup.css'
import type {
  CapacityRow,
  DeploymentDTO,
  Enrollment,
  SetupStatus,
} from '../api/types'

// First run.
//
// The one screen in this product with no header, no roster and no sidebar: on a
// fresh install none of those have anything in them, and a frame around four
// empty panels is a worse first impression than no frame. AppShell renders this
// instead of the chrome, not inside it.
//
// **Nothing here computes a fact about hardware, memory or models.** Every
// number on this screen is read from an endpoint that already owns it:
// `/api/setup` for the machine, `/api/capacity` for what will run on it and how
// fast, `POST /api/enroll` for the join command and the coordinator's own
// address. That is not tidiness -- a second opinion about whether a model fits,
// computed here so the wizard could look confident, would be a promise the
// launch path never agreed to, and the launch is what a person does next.
//
// Which is also why a refusal is printed exactly as it arrives. CLAUDE.md:
// "Planner and fit strings are the product ... A refusal names what to change,
// and rewriting it destroys the thing that made it useful."

type StepId = 'machine' | 'cloud' | 'model' | 'machines' | 'done'

/** The capacity answer, prefetched by the screen and read by the model step.
 *
 *  A union rather than four loose pieces of state, so "still loading" and
 *  "answered with nothing" cannot be confused -- an empty row list is a real
 *  answer about a machine nothing fits on, and rendering a spinner for it would
 *  wait forever for something that already arrived. */
type CapacityAnswer =
  | { state: 'loading' }
  | { state: 'error'; message: string }
  | {
      state: 'ready'
      rows: CapacityRow[]
      basis: 'live' | 'static' | null
      servable: boolean
      unavailable: string | null
      unresolved: { model_id: string; reason: string }[]
    }

interface Choices {
  machine: string | null
  key: 'skipped' | 'added' | null
  model: { row: CapacityRow; launched: boolean } | null
  joined: 'skipped' | 'joined' | null
}

/** What a first run offers, smallest first. Every one of them 4-bit already.
 *
 *  Deliberately NOT the cluster's curated shortlist. That list exists to answer
 *  "what can this hardware do", so it reaches for the ceiling on purpose --
 *  gpt-oss-120b and DeepSeek-V3 are on it, and on one GB10 both come back as a
 *  refusal. Correct answers, wrong screen: a first run that opens with two
 *  paragraphs about being 617 GiB over budget has taught somebody that the
 *  product mostly says no, before they have run anything at all.
 *
 *  So: models that fit, in a range where the choice is about what you want
 *  rather than what is possible. One 30B at the top, because it is the biggest
 *  thing a single Spark runs well and somebody should see that it is offered;
 *  everything else is small enough to download over a coffee. `capacityFor`
 *  replaces the curated walk rather than extending it, so this is the whole
 *  list and the verdicts still come from the same gate a launch goes through.
 *
 *  Every id names a repository that is ALREADY int4 -- AWQ, GPTQ or
 *  compressed-tensors w4a16 -- rather than a bf16 checkpoint the ladder steps
 *  down from, and that is the whole point of this list. The weights are about
 *  a quarter of the bf16 download, which is the wait a first run actually
 *  feels: the pick is followed immediately by the runtime fetching the model,
 *  and nothing on screen moves until it lands. Measured off the hub, for the
 *  two ends of this list -- Qwen3-30B-A3B ships 56.9 GiB of safetensors and
 *  its GPTQ-Int4 build 15.8 GiB; Mistral-7B-Instruct-v0.3 ships 27.0 GiB
 *  (fp32 consolidated plus bf16 shards) against 3.9 GiB for the w4a16 one.
 *  Two other things fall out of it, both load-bearing:
 *
 *  - No requantization. `_rungs` starts at a shape's native dtype, so on a
 *    tight live budget the bf16 list came back as q8_0 / q6_k / q2_k rows --
 *    GGUF rungs that `resolver/support.py` marks UNVERIFIED on vLLM and
 *    UNSUPPORTED on SGLang. awq_int4 and gptq_int4 are SUPPORTED on both, so
 *    what the screen offers is what the runtime actually loads.
 *  - The ladder can still descend BELOW int4 if it has to; it just can no
 *    longer climb back to bf16. This list narrows the starting rung, it does
 *    not second-guess the gate.
 *
 *  meta-llama/Llama-3.1-8B-Instruct stops being the obvious omission here.
 *  The gated repo still resolves to a 401 without an HF_TOKEN and would still
 *  land in `unresolved` -- an apology on the first screen, for a model nobody
 *  can fetch -- but RedHatAI's int4 redistribution of it is ungated, so the
 *  model people ask for by name is on the list after all. Both RedHatAI ids
 *  were checked anonymously: `gated: false`, and they resolve with no token
 *  set on the coordinator. That is the bar for adding anything here. */
const FIRST_RUN_MODELS = [
  'Qwen/Qwen2.5-0.5B-Instruct-GPTQ-Int4',
  'Qwen/Qwen3-4B-AWQ',
  'RedHatAI/Mistral-7B-Instruct-v0.3-quantized.w4a16',
  'RedHatAI/Meta-Llama-3.1-8B-Instruct-quantized.w4a16',
  'Qwen/Qwen3-30B-A3B-GPTQ-Int4',
]

const GB = 1024 ** 3

function gib(bytes: number): string {
  return `${(bytes / GB).toFixed(1)} GB`
}

/** Rows to offer, and the side of the report they came from.
 *
 *  `live` is what the machine can hand out now; `static` is the ceiling from
 *  the spec sheet. They can disagree about which quantization fits and so about
 *  how fast a model decodes, and showing one number from each side would be two
 *  answers to one question. Live wins where it exists -- it is the budget an
 *  actual launch is checked against. */
function rowsOf(report: {
  live: { rows: CapacityRow[] } | null
  static?: { rows: CapacityRow[] } | null
}): { rows: CapacityRow[]; basis: 'live' | 'static' | null } {
  if (report.live?.rows?.length) return { rows: report.live.rows, basis: 'live' }
  if (report.static?.rows?.length) return { rows: report.static.rows, basis: 'static' }
  return { rows: [], basis: null }
}

export function SetupTab() {
  const { backend } = useBackend()
  const { navigate } = useRouter()

  const [status, setStatus] = useState<SetupStatus | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  // Started on mount, not when the model step appears. Resolving five models
  // means five hub round trips on a cold cache, and the person is going to
  // spend a minute on the machine and provider steps regardless -- so the
  // answer is usually already here by the time they ask for it. Prefetching is
  // free: the report is read-only and nothing acts on it until they pick.
  const [capacity, setCapacity] = useState<CapacityAnswer>({ state: 'loading' })
  const [step, setStep] = useState<StepId>('machine')
  const [chose, setChose] = useState<Choices>({
    machine: null,
    key: null,
    model: null,
    joined: null,
  })

  useEffect(() => {
    let live = true
    backend
      .setup()
      .then((s) => live && setStatus(s))
      .catch((err) => live && setLoadError(String(err?.message ?? err)))
    return () => {
      live = false
    }
  }, [backend])

  useEffect(() => {
    let live = true
    // Null context and concurrency: that asks the coordinator to choose them
    // per model out of what actually fits, which is the default path and the
    // only one that gives honest verdicts on a fresh install with nothing
    // configured. No machine list -- the default is the coordinator's own host,
    // which is the machine this screen is about.
    backend
      .capacityFor(FIRST_RUN_MODELS, null, null)
      .then((report) => {
        if (!live) return
        const picked = rowsOf(report)
        setCapacity({
          state: 'ready',
          // Smallest first. A first run should open on the one that finishes
          // downloading soonest, not on whichever sorts first.
          rows: [...picked.rows].sort((a, b) => a.total_params - b.total_params),
          basis: picked.basis,
          servable: report.local_serving,
          unavailable: report.unavailable_reason,
          // Named but unresolvable -- a gated repo, a hub that did not answer.
          // Said out loud rather than silently dropped: a list that quietly
          // shrinks is indistinguishable from one that was always that short.
          unresolved: report.unresolved ?? [],
        })
      })
      .catch((err) => live && setCapacity({ state: 'error', message: String((err as Error)?.message ?? err) }))
    return () => {
      live = false
    }
  }, [backend])

  // The cloud step is not rendered at all when no provider kind can be routed
  // to -- not disabled, not explained. A step nobody can take is noise on the
  // one screen where every word is being read.
  const order = useMemo<StepId[]>(
    () =>
      status?.provider_routing
        ? ['machine', 'cloud', 'model', 'machines', 'done']
        : ['machine', 'model', 'machines', 'done'],
    [status?.provider_routing],
  )

  const goTo = useCallback(
    (next: StepId) => {
      // Rewinding through the ledger clears everything after that point: a tick
      // that survived the answer it was about would be a lie in the one place
      // the reader is using to keep track.
      const at = order.indexOf(next)
      setChose((prev) => ({
        machine: at <= order.indexOf('machine') ? null : prev.machine,
        key: at <= order.indexOf('cloud') ? null : prev.key,
        model: at <= order.indexOf('model') ? null : prev.model,
        joined: at <= order.indexOf('machines') ? null : prev.joined,
      }))
      setStep(next)
      window.scrollTo({ top: 0 })
    },
    [order],
  )

  const advance = useCallback(
    (from: StepId) => {
      const next = order[order.indexOf(from) + 1]
      if (next) {
        setStep(next)
        window.scrollTo({ top: 0 })
      }
    },
    [order],
  )

  if (loadError) {
    return (
      <div className="setup">
        <Head crumb="setting up" />
        <main className="setup-main">
          <h1>This coordinator did not answer.</h1>
          <p className="setup-lede">
            Setup asks it what machine it is running on before it shows you anything, and
            that request failed. The coordinator may still be starting.
          </p>
          <p className="setup-warn setup-fault">{loadError}</p>
          <button className="btn primary" onClick={() => window.location.reload()}>
            Try again
          </button>
        </main>
      </div>
    )
  }

  if (!status) {
    return (
      <div className="setup">
        <Head crumb="setting up" />
        <main className="setup-main">
          <p className="setup-note">Looking at this machine…</p>
        </main>
      </div>
    )
  }

  const index = order.indexOf(step)
  const crumb = step === 'done' ? 'ready' : `step ${index + 1} of ${order.length - 1}`

  return (
    <div className="setup">
      <Head crumb={crumb} />
      <main className="setup-main">
        <Ledger chose={chose} status={status} onBack={goTo} />

        {step === 'machine' && (
          <MachineStep
            status={status}
            onUse={(name) => {
              setChose((p) => ({ ...p, machine: name }))
              advance('machine')
            }}
            onSkip={() => {
              setChose((p) => ({ ...p, machine: null }))
              advance('machine')
            }}
          />
        )}

        {step === 'cloud' && (
          <CloudStep
            onDone={(outcome) => {
              setChose((p) => ({ ...p, key: outcome }))
              advance('cloud')
            }}
          />
        )}

        {step === 'model' && (
          <ModelStep
            answer={capacity}
            onDone={(model) => {
              setChose((p) => ({ ...p, model }))
              advance('model')
            }}
          />
        )}

        {step === 'machines' && (
          <MachinesStep
            onDone={(outcome) => {
              setChose((p) => ({ ...p, joined: outcome }))
              advance('machines')
            }}
          />
        )}

        {step === 'done' && (
          <DoneStep
            chose={chose}
            status={status}
            onFinish={() => navigate({ dest: 'dash' }, { replace: true })}
          />
        )}
      </main>
    </div>
  )
}

function Head({ crumb }: { crumb: string }) {
  return (
    <header className="setup-head">
      <svg width="30" height="23" viewBox="0 0 64 50" aria-label="derate">
        <path
          d="M26 25 V39 H56"
          fill="none"
          stroke="currentColor"
          strokeWidth="6"
          strokeLinecap="square"
          opacity=".32"
        />
        <path
          d="M8 25 H26 V11 H56"
          fill="none"
          stroke="currentColor"
          strokeWidth="6"
          strokeLinecap="square"
        />
      </svg>
      <span className="setup-wordmark">derate</span>
      <span className="setup-where setup-note mono">{crumb}</span>
    </header>
  )
}

function Ledger({
  chose,
  status,
  onBack,
}: {
  chose: Choices
  status: SetupStatus
  onBack: (step: StepId) => void
}) {
  const rows: [string, string, StepId][] = []
  if (chose.machine) rows.push(['This machine', chose.machine, 'machine'])
  if (status.provider_routing && chose.key) {
    rows.push([
      'Cloud backup',
      chose.key === 'skipped' ? 'skipped for now' : 'connected',
      'cloud',
    ])
  }
  if (chose.model) {
    rows.push([
      'Model',
      chose.model.launched ? chose.model.row.label : `${chose.model.row.label} (not started)`,
      'model',
    ])
  }
  if (chose.joined) {
    rows.push([
      'Other machines',
      chose.joined === 'skipped' ? 'none yet' : 'one joined',
      'machines',
    ])
  }

  return (
    <div className="setup-ledger">
      {rows.map(([what, detail, step]) => (
        <div className="setup-done" key={what}>
          <span className="setup-tick" aria-hidden="true">
            ✓
          </span>
          <span className="setup-what">
            <span className="label">{what}</span>
            <span className="setup-detail"> — {detail}</span>
          </span>
          <button className="btn" onClick={() => onBack(step)}>
            Change
          </button>
        </div>
      ))}
    </div>
  )
}

function MachineStep({
  status,
  onUse,
  onSkip,
}: {
  status: SetupStatus
  onUse: (name: string) => void
  onSkip: () => void
}) {
  const machine = status.machine

  if (!machine) {
    // The server declines to name a machine it cannot identify. Saying so beats
    // introducing somebody else's GPU as the box under the reader's desk.
    return (
      <div className="setup-step">
        <h1>This coordinator has not identified its own hardware yet.</h1>
        <p className="setup-lede">
          It is running, and it can already front a cloud provider and accept other
          machines. It just cannot say which of the {status.cluster.nodes} machines on the
          roster is itself, so there is nothing to show you here.
        </p>
        <div className="setup-row">
          <button className="btn primary" onClick={onSkip}>
            Carry on
          </button>
        </div>
      </div>
    )
  }

  const p = machine.profile
  const identified = machine.eligible
  const memory = p.addressable_memory || p.total_memory

  return (
    <div className="setup-step">
      <h1>
        {identified
          ? 'This machine is ready to serve models.'
          : 'This machine is running, but its hardware is not confirmed.'}
      </h1>
      <p className="setup-lede">
        Derate looked at the hardware it is running on. Nothing has been set up yet, and
        nothing is running.
      </p>

      <div className="setup-plate">
        <div className="setup-plate-name mono">{p.gpu_name || p.hostname}</div>
        <div className="setup-plate-spec mono">
          {memory > 0 ? gib(memory) : 'memory not reported'}
          {p.memory_bandwidth_gbps > 0
            ? ` · ${p.memory_bandwidth_gbps.toFixed(0)} GB/s memory bandwidth`
            : ''}
          {p.gpu_count > 1 ? ` · ${p.gpu_count} GPUs` : ''}
        </div>
        {/* No "large enough for roughly N billion parameters" line. That number
            would be this screen's own arithmetic over an assumed quantization,
            and the next step answers the same question properly, per model,
            from the gate a launch actually goes through. */}
        <div className="setup-plate-caption">
          {identified
            ? 'What will actually run on it is the next step.'
            : machine.ineligible_reason}
        </div>
      </div>

      <div className="setup-row">
        <button
          className="btn primary"
          onClick={() => onUse(`${p.gpu_name || p.hostname}${memory > 0 ? `, ${gib(memory)}` : ''}`)}
        >
          Use this machine
        </button>
        <button className="setup-skip" onClick={onSkip}>
          I only want cloud models
        </button>
      </div>
    </div>
  )
}

function CloudStep({ onDone }: { onDone: (outcome: 'added' | 'skipped') => void }) {
  const { backend, invalidate } = useBackend()
  const [key, setKey] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const add = async () => {
    if (!key.trim()) return
    setBusy(true)
    setError(null)
    try {
      await backend.addProvider({ kind: 'openrouter', api_key: key.trim() })
      // Cluster state changed: every polled resource should see it now rather
      // than waiting out its interval.
      invalidate()
      onDone('added')
    } catch (err) {
      setError(String((err as Error)?.message ?? err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="setup-step">
      <h1>Add a cloud provider as backup?</h1>
      <p className="setup-lede">
        Optional. With one connected, derate keeps using your machine and only reaches for
        the cloud when your machine is already busy.
      </p>

      <div className="setup-box">
        <div className="setup-line">
          <span className="setup-dot on" />
          <span className="setup-who">Your machine</span>
          <span className="setup-says">takes every request it has room for</span>
        </div>
        <div className="setup-line">
          <span className="setup-dot off" />
          <span className="setup-who">OpenRouter</span>
          <span className="setup-says">takes the overflow, so nothing waits in a queue</span>
        </div>
      </div>

      <label className="label" htmlFor="setup-key" style={{ display: 'block', marginBottom: 'var(--s-2)' }}>
        OpenRouter key
      </label>
      <input
        id="setup-key"
        className="setup-field"
        type="password"
        placeholder="sk-or-v1-..."
        autoComplete="off"
        value={key}
        onChange={(e) => setKey(e.target.value)}
      />
      <p className="setup-note" style={{ margin: 'var(--s-2) 0 var(--s-5)' }}>
        Stored on the coordinator and never shown again, not even to you.
      </p>

      {error && <p className="setup-warn setup-fault">{error}</p>}

      <div className="setup-row">
        <button className="btn primary" onClick={() => void add()} disabled={busy || !key.trim()}>
          {busy ? 'Adding…' : 'Add key'}
        </button>
        <button className="setup-skip" onClick={() => onDone('skipped')}>
          Skip — local only
        </button>
      </div>
    </div>
  )
}

function ModelStep({
  answer,
  onDone,
}: {
  answer: CapacityAnswer
  onDone: (model: { row: CapacityRow; launched: boolean } | null) => void
}) {
  const { backend, invalidate } = useBackend()
  const [launching, setLaunching] = useState<{
    row: CapacityRow
    deployment: DeploymentDTO | null
    error: string | null
  } | null>(null)
  // How long this step has been waiting, so a slow answer can say so instead
  // of spinning. Only ticks while there is nothing to show.
  const [waited, setWaited] = useState(0)

  useEffect(() => {
    if (answer.state !== 'loading') return
    const t = window.setInterval(() => setWaited((n) => n + 1), 1000)
    return () => window.clearInterval(t)
  }, [answer.state])

  const start = (row: CapacityRow) => {
    // Deliberately NOT awaited before rendering. `launch` returns as soon as the
    // process is spawned -- the readiness wait runs on a thread server-side --
    // but "as soon as" still covers a resolve, the fit gate and a process
    // start, and until this returned the screen greyed every control out and
    // said "Starting…" in a footnote. That reads as frozen, which is the one
    // impression to avoid at the exact moment somebody has committed to
    // downloading tens of gigabytes.
    setLaunching({ row, deployment: null, error: null })
    void run(row)
  }

  const run = async (row: CapacityRow) => {
    try {
      // The context and concurrency the verdict was taken at, not numbers this
      // screen chose: launching at a different shape than the one shown to fit
      // would make the verdict on screen a fiction about a different request.
      const dep = await backend.launch({
        model_id: row.model_id,
        context: row.context,
        concurrency: row.max_seqs,
        target: 'throughput',
        // The gateway's own default when a caller names none. Named here
        // because LaunchRequest requires it, not because this screen has an
        // opinion about runtimes.
        runtime: 'vllm',
      })
      invalidate()
      setLaunching((l) => (l ? { ...l, deployment: dep } : l))
    } catch (err) {
      // Verbatim. A refusal from the fit gate names what to change, and this
      // screen is not entitled to summarise it.
      setLaunching((l) => (l ? { ...l, error: String((err as Error)?.message ?? err) } : l))
    }
  }

  if (launching) {
    return (
      <LaunchProgress
        row={launching.row}
        deployment={launching.deployment}
        error={launching.error}
        onBack={() => setLaunching(null)}
        onContinue={() => onDone({ row: launching.row, launched: true })}
      />
    )
  }

  if (answer.state === 'error') {
    return (
      <div className="setup-step">
        <h1>Could not work out what runs here.</h1>
        <p className="setup-warn setup-fault">{answer.message}</p>
        <button className="btn primary" onClick={() => onDone(null)}>
          Skip for now
        </button>
      </div>
    )
  }

  if (answer.state === 'loading') {
    return (
      <div className="setup-step">
        <h1>Pick something to run.</h1>
        <p className="setup-lede">
          Reading each model&rsquo;s configuration from Hugging Face to see what fits. This
          is a one-time lookup per model; it is cached afterwards.
        </p>
        {/* Named, and bounded. An indefinite spinner cannot be told apart from
            a hung request, and this one depends on a third party being up. */}
        {waited >= 10 && (
          <>
            <p className="setup-warn">
              Still waiting after {waited} seconds. Hugging Face may be slow or
              unreachable from this machine. Nothing is stuck on your side, and you can
              come back to this from the Models screen.
            </p>
            <button className="setup-skip" onClick={() => onDone(null)}>
              Skip &mdash; I will pick one later
            </button>
          </>
        )}
      </div>
    )
  }

  const { rows, basis, servable, unavailable, unresolved } = answer
  const fitting = rows.filter((r) => r.fits)

  return (
    <div className="setup-step">
      <h1>Pick something to run.</h1>
      <p className="setup-lede">
        {fitting.length > 0
          ? 'These fit on your machine. You can add more later, and run several at once.'
          : 'Nothing here fits this machine yet. You can still add a cloud provider, or another machine, and come back to this.'}
      </p>

      {unavailable && <p className="setup-warn">{unavailable}</p>}

      {unresolved.map((u) => (
        <p className="setup-warn" key={u.model_id}>
          <span className="mono">{u.model_id}</span> could not be read: {u.reason}
        </p>
      ))}

      {!servable && (
        <p className="setup-warn">
          These verdicts are real, but this coordinator cannot start a model itself &mdash;
          it has no local runtime. It can still route to a cloud provider, and to other
          machines that join it.
        </p>
      )}

      <div className="setup-picks">
        {rows.map((row) => (
          <button
            key={row.model_id}
            className="setup-pick"
            disabled={!row.fits || !servable}
            onClick={() => void start(row)}
          >
            <span>
              <span className="setup-pick-title">{row.label}</span>
              <span className="setup-pick-desc">{row.reason}</span>
            </span>
            <span className="setup-facts">
              {/* The speed sits beside the verdict, never alone: on a row that
                  does not fit it is the rate the model WOULD decode at, which
                  is a hypothesis, and printing it on its own would read as a
                  promise.

                  The context is printed with it because the two are not
                  separable. Each row is judged at its own context -- the gate
                  fills the memory each model leaves free -- so decoding one
                  token reads a different amount of cache per row. Without the
                  denominator these numbers look comparable and are not. */}
              {row.predicted_decode_tps !== null ? (
                <b title="tokens per second — roughly how fast it writes. 15 or so is reading speed.">
                  {row.predicted_decode_tps.toFixed(0)} tok/sec
                </b>
              ) : (
                <b className="no">no estimate</b>
              )}
              {row.fits ? (row.dtype ?? 'fits') : <span className="no">will not fit</span>}
              <span className="setup-ctx">at {Math.round(row.context / 1024)}k context</span>
            </span>
          </button>
        ))}
      </div>

      <div className="setup-row">
        <button className="setup-skip" onClick={() => onDone(null)}>
          Skip &mdash; I will pick one later
        </button>
      </div>
      <p className="setup-note" style={{ marginTop: 'var(--s-3)' }}>
        {basis === 'live'
          ? 'Measured against the memory this machine can hand out right now. '
          : basis === 'static'
            ? 'Measured against this machine\u2019s stated memory ceiling; no live reading yet. '
            : ''}
        Speeds are a memory-bandwidth estimate at each row\u2019s own context, not a
        benchmark.
      </p>
    </div>
  )
}

/** What happens after somebody picks a model, in the phases it actually goes
 *  through.
 *
 *  "launching" for four minutes is technically true and useless: it covers a
 *  container image, a multi-gigabyte download, a load onto the GPU and an
 *  engine that compiles itself before it will answer. They fail differently,
 *  take different lengths of time and want different patience.
 *
 *  This screen used to infer the phases here, from the repo's bytes on disk:
 *  bytes appearing meant downloading, bytes holding still for two polls meant
 *  loading. It worked, and it was the only thing that could have worked, but
 *  it could not tell an engine compiling from a download stalled -- both are a
 *  number that stopped moving. The server reads the phases now, off sparkrun's
 *  own output and the backend's own log (`/api/activity`, from
 *  control_plane/deploy/progress.py), so this screen and the rail agree and
 *  neither is guessing.
 *
 *  What stays is the byte figure, because it is the one number a person can
 *  act on while the weights land: it is the real size of the repo in the
 *  node's Hugging Face cache, with the rate measured between two polls of it.
 *  There is still no percentage for it -- nothing reports the repo's final
 *  size before it arrives -- and a bar creeping at a made-up rate gets read as
 *  an estimate and planned around. A number that goes up, with a rate beside
 *  it, says "working" without claiming to know when it stops.
 */
type Phase = LaunchPhase | 'failed'

const PHASE_LABEL: Record<Phase, string> = {
  ...LAUNCH_PHASE_LABEL,
  failed: 'Failed',
}

const PHASE_ORDER: Phase[] = [...LAUNCH_PHASES]

function LaunchProgress({
  row,
  deployment,
  error,
  onBack,
  onContinue,
}: {
  row: CapacityRow
  deployment: DeploymentDTO | null
  error: string | null
  onBack: () => void
  onContinue: () => void
}) {
  const { backend } = useBackend()
  const [elapsed, setElapsed] = useState(0)
  const [phase, setPhase] = useState<Phase>('preparing')
  const [bytes, setBytes] = useState<number | null>(null)
  const [rate, setRate] = useState<number | null>(null)
  const [failure, setFailure] = useState<string | null>(null)
  const [startedAt, setStartedAt] = useState<number | null>(null)
  // The launcher's or the runtime's own sentence about what it is doing right
  // now, rendered exactly as it arrives.
  const [says, setSays] = useState('')
  // 0..1 while the runtime is counting its checkpoint shards onto the GPU, and
  // null every other second of a launch, because nothing else reports one.
  const [fraction, setFraction] = useState<number | null>(null)
  // When each phase was first seen, so the stepper can show how long each took
  // rather than one clock for the whole thing.
  const [reached, setReached] = useState<Partial<Record<Phase, number>>>({ preparing: 0 })

  useEffect(() => {
    const t = window.setInterval(() => setElapsed((n) => n + 1), 1000)
    return () => window.clearInterval(t)
  }, [])

  useEffect(() => {
    if (error) return
    let live = true
    let timer = 0
    let lastBytes: number | null = null
    let lastAt = 0

    const poll = async () => {
      try {
        // Storage only while the weights could still be arriving. It fans out
        // to every node agent and does real syscalls there -- the rail polls
        // it at thirty seconds for that reason -- and this screen asks every
        // two. That was the only way to see a download before the server
        // reported one; once the phase says the bytes have landed, the figure
        // cannot change and the request is pure cost on the machine that is
        // busy loading a model.
        const wantBytes =
          phaseRef.current === 'preparing' || phaseRef.current === 'downloading'
        const [deps, activity, storage] = await Promise.all([
          backend.deployments(),
          backend.activity(),
          wantBytes ? backend.storage() : Promise.resolve(null),
        ])
        if (!live) return

        const mine = deps.find(
          (d) => d.model_id === row.model_id || d.deployment_id === deployment?.deployment_id,
        )
        // What the server read off sparkrun and off the backend's own log.
        const arriving = (activity.launches ?? []).find(
          (l) =>
            l.deployment_id === deployment?.deployment_id || l.model_id === row.model_id,
        )

        // Bytes on disk for this repo, across whichever node holds it.
        let onDisk: number | null = null
        for (const node of storage?.nodes ?? []) {
          const repo = node.models?.repos?.find((r) => r.repo_id === row.model_id)
          if (repo) onDisk = (onDisk ?? 0) + repo.bytes
        }
        const now = Date.now()
        if (onDisk !== null) {
          setBytes(onDisk)
          // What was here before this launch touched anything. If the figure
          // never moves off it, the weights were already cached and no download
          // happened -- worth saying, rather than showing a download step that
          // silently did nothing.
          setStartedAt((b) => (b === null ? onDisk : b))
          if (lastBytes !== null && now > lastAt) {
            const delta = onDisk - lastBytes
            const seconds = (now - lastAt) / 1000
            // Only a rate while it is actually moving. A rate of zero printed
            // beside a stalled number reads as a stall this cannot diagnose.
            setRate(delta > 0 ? delta / seconds : null)
          }
          lastBytes = onDisk
          lastAt = now
        }

        // The sentence and the shard count, straight through. Held rather
        // than cleared when a poll brings nothing: a log tail is a window,
        // and the caption blinking out because one read came back empty is
        // worse than a caption a few seconds stale.
        if (arriving?.status) setSays(arriving.status)
        // The runtime announcing its own death arrives here a poll or two
        // before the deployment record carries the failure, and it says more
        // than the record will: "Free memory on device cuda:0 (49.56/121.69
        // GiB) on startup is less than desired GPU memory utilization" names
        // what to change. Shown as the failure immediately rather than held
        // behind a stepper still ticking towards Serving.
        if (arriving?.fatal && arriving.status) setFailure(arriving.status)
        if (arriving && arriving.fraction != null) setFraction(arriving.fraction)
        else if (arriving?.phase && arriving.phase !== 'loading') setFraction(null)

        // The record's state decides the two ends; the server's reading
        // decides the middle. A launch the activity endpoint has not picked
        // up yet leaves the phase where it is rather than inventing one --
        // `preparing` is where it starts, and it is true from the first
        // second.
        let reading: Phase | null = null
        if (mine?.state === 'ready' || mine?.state === 'degraded') reading = 'serving'
        else if (mine?.state === 'failed') reading = 'failed'
        else if (arriving?.phase) reading = arriving.phase as Phase

        if (reading !== null) {
          const next = reading
          setPhase((prev) => {
            // Phases only go forward. A poll that lands between two writes can
            // otherwise walk backwards, and a stepper that un-ticks a step
            // reads as something going wrong. A phase this build has never
            // heard of sorts to -1 and so is treated as backwards, which is
            // the safe way to be wrong about one.
            if (prev === 'failed' || prev === 'serving') return prev
            const back = PHASE_ORDER.indexOf(next) < PHASE_ORDER.indexOf(prev)
            return next === 'failed' || !back ? next : prev
          })
          setReached((r) => (r[next] === undefined ? { ...r, [next]: elapsedRef.current } : r))
        }
        if (mine?.last_error) setFailure(mine.last_error)
      } catch {
        // A failed poll is not worth interrupting a download for. The next one
        // is two seconds away and the clock keeps running regardless.
      }
      if (live) timer = window.setTimeout(poll, 2000)
    }
    void poll()
    return () => {
      live = false
      window.clearTimeout(timer)
    }
  }, [backend, row.model_id, deployment?.deployment_id, error])

  // Read inside the poll without making it a dependency, which would restart
  // the loop every second and reset the rate measurement with it.
  const elapsedRef = useRef(0)
  elapsedRef.current = elapsed
  // Same reason: the poll decides whether to ask for storage from the phase it
  // is already on, and making the phase a dependency would tear the loop down
  // and rebuild it every time the phase moved.
  const phaseRef = useRef<Phase>('preparing')
  phaseRef.current = phase

  const gb = (n: number) => `${(n / 1024 ** 3).toFixed(1)} GB`
  const mbs = (n: number) => `${(n / 1024 ** 2).toFixed(0)} MB/s`
  const clock = (n: number) => `${Math.floor(n / 60)}:${String(n % 60).padStart(2, '0')}`

  if (error || phase === 'failed') {
    return (
      <div className="setup-step">
        <h1>{row.label} did not start.</h1>
        {/* Verbatim. A refusal from the fit gate or the launcher names what to
            change, and this screen is not entitled to summarise it. */}
        <p className="setup-warn setup-fault">{error ?? failure ?? 'The launcher reported a failure.'}</p>
        <div className="setup-row">
          <button className="btn primary" onClick={onBack}>
            Pick something else
          </button>
          <button className="setup-skip" onClick={onContinue}>
            Carry on anyway
          </button>
        </div>
      </div>
    )
  }

  const done = phase === 'serving'
  const at = PHASE_ORDER.indexOf(phase)
  // Never grew past what was there when we arrived: nothing was fetched, the
  // weights were already cached.
  const cached =
    startedAt !== null &&
    bytes !== null &&
    bytes === startedAt &&
    at > PHASE_ORDER.indexOf('downloading')

  return (
    <div className="setup-step">
      <h1>{done ? `${row.label} is ready.` : `Getting ${row.label} ready.`}</h1>
      <p className="setup-lede">
        {done
          ? 'It is loaded and answering. You can carry on setting up.'
          : phase === 'downloading'
            ? 'Fetching the weights onto this machine. This runs in the background — you can carry on, or close this page and come back.'
            : phase === 'loading'
              ? 'The weights are on disk. Reading them onto the GPU now.'
              : phase === 'starting'
                ? // Not "nearly done". The engine compiles itself and captures
                  // CUDA graphs here, and on the first launch of a model that
                  // is minutes with no download to explain it — which is
                  // exactly when this screen used to go quiet. Later launches
                  // of the same model reuse what was compiled and skip most of
                  // it.
                  'The weights are on the GPU. The engine is compiling and capturing CUDA graphs, which is slowest the first time a model runs here.'
                : 'Getting the machine ready: the runtime container, then the weights.'}
      </p>

      <div className="setup-plate">
        <div className="setup-plate-name mono">{row.label}</div>
        <div className="setup-plate-spec mono">
          {row.dtype ?? 'native'} · {Math.round(row.context / 1024)}k context
          {deployment?.node_ids?.length ? ` · ${deployment.node_ids.join(', ')}` : ''}
        </div>

        {/* Filled only when something measured it: done, or the runtime
            counting its own checkpoint shards onto the GPU. Every other
            second of a launch this is an open track, because a bar moving at
            a rate nobody measured is read as an estimate and planned around. */}
        <div className="setup-bar">
          {done ? (
            <span className="is-known" style={{ width: '100%' }} />
          ) : fraction !== null ? (
            <span className="is-known" style={{ width: `${Math.round(fraction * 100)}%` }} />
          ) : (
            <span className="is-open" />
          )}
        </div>

        <ol className="setup-phases">
          {PHASE_ORDER.map((p, i) => {
            const state = done || i < at ? 'done' : i === at ? 'now' : 'todo'
            return (
              <li key={p} className={`setup-phase is-${state}`}>
                <span className="mark" aria-hidden="true">
                  {state === 'done' ? '✓' : state === 'now' ? '●' : '○'}
                </span>
                <span className={state === 'now' && !done ? 'dotting' : undefined}>
                  {PHASE_LABEL[p]}
                </span>
                <span className="detail mono">
                  {p === 'downloading' && bytes !== null
                    ? cached
                      ? `${gb(bytes)} already on disk`
                      : `${gb(bytes)}${state === 'now' && rate ? ` · ${mbs(rate)}` : ''}`
                    : reached[p] !== undefined
                      ? clock(reached[p] as number)
                      : ''}
                </span>
              </li>
            )
          })}
        </ol>

        {/* The launcher's or the runtime's own words, passed through: "Pulling
            image: ghcr.io/...", "Loading safetensors checkpoint shards: 5/11",
            "Capturing CUDA graphs". Every one of them is more specific than
            the step above it, and rewriting them here would throw away the
            only thing on this screen that says which of several minutes-long
            things is happening right now. */}
        {!done && says && <div className="setup-log mono">{says}</div>}

        <div className="setup-stage setup-plate-spec">
          <span className="elapsed">{clock(elapsed)} elapsed</span>
        </div>
      </div>

      {/* Said plainly rather than left as a mystery. Somebody watching a number
          that is not a percentage deserves to know why it is not one. */}
      {!done && (
        <p className="setup-note" style={{ marginBottom: 'var(--s-5)' }}>
          The size shown is what has actually landed in this machine&rsquo;s model cache.
          There is no percentage for the download because nothing reports its final
          size before it finishes &mdash; the runtime fetches its own weights. The bar
          fills only while the runtime is counting its own shards onto the GPU, which
          is the one step of a launch that measures itself.
        </p>
      )}

      {failure && !done && <p className="setup-warn">{failure}</p>}

      <div className="setup-row">
        <button className="btn primary" onClick={onContinue}>
          {done ? 'Continue' : 'Continue while it loads'}
        </button>
        {!done && (
          <button className="setup-skip" onClick={onBack}>
            Pick something else instead
          </button>
        )}
      </div>
    </div>
  )
}

function MachinesStep({ onDone }: { onDone: (outcome: 'joined' | 'skipped') => void }) {
  const { backend } = useBackend()
  const [enrollment, setEnrollment] = useState<Enrollment | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    let live = true
    // Minted server-side, command and all: the coordinator's address is its own
    // probed one, not `window.location.host`, which is right in production and
    // a lie in dev. A wrong address in an install command fails on a machine
    // nobody is looking at.
    backend
      .mintEnrollment({ auto_admit: true })
      .then((e) => live && setEnrollment(e))
      .catch((err) => live && setError(String((err as Error)?.message ?? err)))
    return () => {
      live = false
    }
  }, [backend])

  return (
    <div className="setup-step">
      <h1>Add another machine?</h1>
      <p className="setup-lede">
        Any other computer on your network can join this one. A gaming desktop, a laptop, a
        Raspberry Pi. Models run wherever they fit, and you keep using the same address.
      </p>

      <p className="label" style={{ marginBottom: 'var(--s-2)' }}>
        Run this on the other machine
      </p>

      {error ? (
        <p className="setup-warn setup-fault">{error}</p>
      ) : (
        <div className="setup-cmd">
          <code>{enrollment ? enrollment.command : 'composing the command…'}</code>
          <button
            className="btn"
            disabled={!enrollment}
            onClick={() => {
              if (!enrollment) return
              void navigator.clipboard?.writeText(enrollment.command)
              setCopied(true)
              window.setTimeout(() => setCopied(false), 1400)
            }}
          >
            {copied ? 'Copied' : 'Copy'}
          </button>
        </div>
      )}

      <p className="setup-note" style={{ marginBottom: 'var(--s-5)' }}>
        This is the only step that needs a terminal, and only on the machine being added.
        Nothing to install on this one. The token in that line expires
        {enrollment ? ` in ${Math.round(enrollment.expires_in_s / 60)} minutes` : ' shortly'}.
      </p>

      <div className="setup-row">
        <button className="btn primary" onClick={() => onDone('joined')}>
          A machine joined
        </button>
        <button className="setup-skip" onClick={() => onDone('skipped')}>
          Skip — just this machine
        </button>
      </div>
    </div>
  )
}

function DoneStep({
  chose,
  status,
  onFinish,
}: {
  chose: Choices
  status: SetupStatus
  onFinish: () => void
}) {
  const { backend } = useBackend()
  const [endpoint, setEndpoint] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)
  const [recordError, setRecordError] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    // The coordinator's own address again, for the same reason the join command
    // uses it: this is the address somebody types into another machine, and the
    // browser's idea of it is only right by coincidence.
    backend
      .mintEnrollment({ ttl_s: 60 })
      .then((e) => live && setEndpoint(`${e.join_url.replace(/\/+$/, '')}/v1`))
      .catch(() => live && setEndpoint(`${window.location.origin}/v1`))
    return () => {
      live = false
    }
  }, [backend])

  useEffect(() => {
    // Recorded once, on arrival. Somebody who reached this screen has answered
    // the question, including if they chose to add nothing.
    backend.completeSetup().catch((err) => setRecordError(String((err as Error)?.message ?? err)))
  }, [backend])

  const spill = status.provider_routing && chose.key === 'added'

  return (
    <div className="setup-step">
      <h1>{chose.model?.launched ? `${chose.model.row.label} is starting.` : 'derate is up.'}</h1>
      <p className="setup-lede">
        {chose.model?.launched
          ? 'It is loading now. This is the address your other apps will use to reach it.'
          : 'Nothing is running yet. This is the address to use once something is.'}
      </p>

      {recordError && <p className="setup-warn">{recordError}</p>}

      <p className="label" style={{ margin: 'var(--s-6) 0 var(--s-2)' }}>
        Use it from your phone or any other app
      </p>
      <div className="setup-handoff">
        {endpoint && <Qr text={endpoint} label={`QR code for ${endpoint}`} />}
        <div>
          <p className="setup-note" style={{ margin: '0 0 var(--s-3)' }}>
            Scan to open derate on your phone. Nothing to type, no IP address to copy.
          </p>
          <div className="setup-endpoint" style={{ margin: 0 }}>
            <code>{endpoint ?? 'finding this coordinator’s address…'}</code>
            <button
              className="btn"
              disabled={!endpoint}
              onClick={() => {
                if (!endpoint) return
                void navigator.clipboard?.writeText(endpoint)
                setCopied(true)
                window.setTimeout(() => setCopied(false), 1400)
              }}
            >
              {copied ? 'Copied' : 'Copy'}
            </button>
          </div>
          {chose.model && (
            <p className="setup-note" style={{ margin: 'var(--s-2) 0 0' }}>
              Model name <span className="mono">{chose.model.row.model_id}</span>. No other
              settings.
            </p>
          )}
        </div>
      </div>

      <div className="setup-box" style={{ margin: 'var(--s-6) 0 var(--s-5)' }}>
        <div className="setup-line">
          <span className={`setup-dot ${chose.model?.launched ? 'on' : 'off'}`} />
          <span className="setup-who">Your machine</span>
          <span className="setup-says">
            {chose.model?.launched ? 'starting a model now' : 'no model running yet'}
          </span>
        </div>
        {status.provider_routing && (
          <div className="setup-line">
            <span className={`setup-dot ${spill ? 'on' : 'off'}`} />
            <span className="setup-who">Cloud</span>
            <span className="setup-says">
              {spill
                ? 'standing by, takes over only when your machine is full'
                : 'not connected — add a key in Settings to handle overflow'}
            </span>
          </div>
        )}
      </div>

      <button className="btn primary" onClick={onFinish}>
        Open the dashboard
      </button>
    </div>
  )
}
