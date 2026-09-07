import { useCallback, useEffect, useMemo, useState } from 'react'
import { useBackend } from '../state/backend'
import { useRouter } from '../state/router'
import { Qr } from './setup/Qr'
import './setup/setup.css'
import type { CapacityRow, Enrollment, SetupStatus } from '../api/types'

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

interface Choices {
  machine: string | null
  key: 'skipped' | 'added' | null
  model: { row: CapacityRow; launched: boolean } | null
  joined: 'skipped' | 'joined' | null
}

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
  onDone,
}: {
  onDone: (model: { row: CapacityRow; launched: boolean } | null) => void
}) {
  const { backend, invalidate } = useBackend()
  const [rows, setRows] = useState<CapacityRow[] | null>(null)
  const [basis, setBasis] = useState<'live' | 'static' | null>(null)
  const [servable, setServable] = useState(true)
  const [unavailable, setUnavailable] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [starting, setStarting] = useState<string | null>(null)
  const [refusal, setRefusal] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    // Both arguments null: that asks the coordinator to choose a context per
    // model from what actually fits, which is the default path and the only one
    // that gives honest verdicts on a fresh install with nothing configured.
    // No machine list -- the default is the coordinator's own host, which is
    // the machine this screen is about.
    backend
      .capacity(null, null)
      .then((report) => {
        if (!live) return
        const picked = rowsOf(report)
        setRows(picked.rows)
        setBasis(picked.basis)
        setServable(report.local_serving)
        setUnavailable(report.unavailable_reason)
      })
      .catch((err) => live && setError(String((err as Error)?.message ?? err)))
    return () => {
      live = false
    }
  }, [backend])

  const start = async (row: CapacityRow) => {
    setStarting(row.model_id)
    setRefusal(null)
    try {
      // The context and concurrency the verdict was taken at, not numbers this
      // screen chose: launching at a different shape than the one that was
      // shown "fits" would make the verdict on screen a fiction.
      await backend.launch({
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
      onDone({ row, launched: true })
    } catch (err) {
      // Verbatim. A refusal from the fit gate names what to change, and this
      // screen is not entitled to summarise it.
      setRefusal(String((err as Error)?.message ?? err))
      setStarting(null)
    }
  }

  if (error) {
    return (
      <div className="setup-step">
        <h1>Could not work out what runs here.</h1>
        <p className="setup-warn setup-fault">{error}</p>
        <button className="btn primary" onClick={() => onDone(null)}>
          Skip for now
        </button>
      </div>
    )
  }

  if (!rows) {
    return (
      <div className="setup-step">
        <h1>Pick something to run.</h1>
        <p className="setup-note">Working out what fits on this machine…</p>
      </div>
    )
  }

  const fitting = rows.filter((r) => r.fits)

  return (
    <div className="setup-step">
      <h1>Pick something to run.</h1>
      <p className="setup-lede">
        {fitting.length > 0
          ? 'These fit on your machine. You can add more later, and run several at once.'
          : 'Nothing on the shortlist fits this machine yet. You can still add a cloud provider, or another machine, and come back to this.'}
      </p>

      {unavailable && <p className="setup-warn">{unavailable}</p>}

      {!servable && (
        <p className="setup-warn">
          These verdicts are real, but this coordinator cannot start a model itself — it
          has no local runtime. It can still route to a cloud provider, and to other
          machines that join it.
        </p>
      )}

      <div className="setup-picks">
        {rows.map((row) => (
          <button
            key={row.model_id}
            className="setup-pick"
            disabled={!row.fits || !servable || starting !== null}
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
                  promise. */}
              {row.predicted_decode_tps !== null ? (
                <b title="tokens per second — roughly how fast it writes. 15 or so is reading speed.">
                  {row.predicted_decode_tps.toFixed(0)} tok/sec
                </b>
              ) : (
                <b className="no">no estimate</b>
              )}
              {row.fits ? (row.dtype ?? 'fits') : <span className="no">will not fit</span>}
            </span>
          </button>
        ))}
      </div>

      {refusal && <p className="setup-warn setup-fault">{refusal}</p>}

      <div className="setup-row">
        <button className="setup-skip" onClick={() => onDone(null)}>
          Skip — I will pick one later
        </button>
      </div>
      <p className="setup-note" style={{ marginTop: 'var(--s-3)' }}>
        {basis === 'live'
          ? 'Measured against the memory this machine can hand out right now.'
          : basis === 'static'
            ? 'Measured against this machine’s stated memory ceiling; no live reading yet.'
            : ''}
        {starting ? ' Starting…' : ''}
      </p>
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
