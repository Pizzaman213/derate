/** The Advanced tab's second-pass sections: the profile bar, the typed engine
 *  knobs, lifecycle, environment rows and the command preview.
 *
 *  Same rule as `AdvancedTab`: no state lives here. Every value is read off
 *  and written through `useModelSession`, so the plan, the preview and the
 *  launch all see one copy. */

import { useEffect, useId, useState } from 'react'
import type { EngineOptions, Lifecycle, LifecyclePriority, RuntimeInfo, SpeculativeHead } from '../../api/types'
import { KEY_EXPIRY_DAYS } from '../../api/types'
import { Copyable } from '../../components/Copyable'
import { Lamp } from '../../components/Lamp'
import { PasswordField } from '../../components/PasswordField'
import { RevealModal } from '../../components/RevealModal'
import { Select, type SelectOption } from '../../components/Select'
import { Verbatim } from '../../components/Verbatim'
import { useBackend } from '../../state/backend'
import { useRouter } from '../../state/router'
import { profilePill } from './profiles'
import { gib } from './verdictView'
import { refFor } from './speculative'
import type { ModelSession } from './useModelSession'

const errText = (e: unknown) => (e instanceof Error ? e.message : String(e))

/** Merge a patch into the typed options, dropping every key that went back
 *  to its default -- an absent key IS the default on the wire. */
export function withOption(opts: EngineOptions, patch: EngineOptions): EngineOptions {
  const out: Record<string, unknown> = { ...opts, ...patch }
  for (const [k, v] of Object.entries(out)) {
    if (v == null || v === false || v === '' || (typeof v === 'object' && !Object.keys(v).length)) delete out[k]
  }
  return out as EngineOptions
}

// ── Profile bar ─────────────────────────────────────────────────────────────

export function ProfileBar({ s }: { s: ModelSession }) {
  const { backend } = useBackend()
  const uid = useId()
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const modelId = s.detail?.model_id ?? s.subjectId

  const status = s.modified
    ? 'Changed since loading'
    : s.profileSource === 'default'
      ? 'Preloaded: your starred default'
      : s.profileSource === 'last-run'
        ? 'Preloaded: your last run'
        : s.profileSource === 'loaded'
          ? 'Loaded by you'
          : 'Planner defaults'
  const signal = s.modified ? 'warn' : s.profileSource ? 'live' : 'idle'

  const options: SelectOption<string>[] = [
    { value: '', label: 'Planner defaults' },
    ...s.profiles.map((p) => {
      const failed = profilePill(p, s.deployments) === 'failed run'
      return {
        value: p.profile_id,
        label: `${p.is_default ? '★ ' : ''}${p.name}${failed ? ' (failed run)' : ''}`,
        ...(failed ? { disabled: true, title: 'This run failed; its settings are not offered for loading.' } : {}),
      }
    }),
  ]

  const run = async (f: () => Promise<void>) => {
    setBusy(true)
    setErr(null)
    try {
      await f()
    } catch (e) {
      setErr(errText(e))
    } finally {
      setBusy(false)
    }
  }

  const saveNew = () => {
    const name = window.prompt('Name this setup', `Saved setup ${s.profiles.length + 1}`)?.trim()
    if (!name) return
    void run(async () => {
      s.markSaved(await backend.createProfile({ model_id: modelId, name, request: s.currentRequest() }))
    })
  }

  const active = s.activeProfile
  return (
    <div className="mdl-pbar">
      <div className="mdl-pbar-row">
        <div className="fld mdl-fld-inline">
          <label htmlFor={`${uid}-profile`}>Setup</label>
          <Select
            id={`${uid}-profile`}
            value={active?.profile_id ?? ''}
            options={options}
            onChange={(id) => {
              const p = s.profiles.find((x) => x.profile_id === id)
              if (p) s.applyRequest(p.request, p)
              else s.resetToDefaults()
            }}
          />
        </div>
        <span className="mdl-pbar-status unit">
          <Lamp signal={signal} label={status} />
          {status}
        </span>
        <span className="mdl-pbar-actions">
          {active && s.modified ? (
            <button
              type="button"
              disabled={busy}
              onClick={() =>
                void run(async () => {
                  s.markSaved(await backend.patchProfile(active.profile_id, { request: s.currentRequest() }))
                })
              }
            >
              {`Update "${active.name}"`}
            </button>
          ) : null}
          {s.profileBase && s.modified ? (
            <button type="button" className="ghost" onClick={() => s.applyRequest(s.profileBase!, active)}>
              Revert
            </button>
          ) : null}
          <button type="button" className="ghost" disabled={busy} onClick={saveNew}>
            Save as new
          </button>
          <button type="button" className="ghost" onClick={s.resetToDefaults}>
            Planner defaults
          </button>
        </span>
      </div>
      {s.changed.length ? <p className="unit">{`Changed: ${s.changed.join(', ')}`}</p> : null}
      {err ? <Verbatim text={err} size="unit" /> : null}
    </div>
  )
}

// ── Engine facts ────────────────────────────────────────────────────────────

const SUPPORT_LABEL: Record<keyof RuntimeInfo['supports'], string> = {
  speculative: 'speculative',
  graph_capture: 'CUDA graphs',
  kv_dtype: 'kv width',
  quantization: 'forced quant',
  expert_parallel: 'expert parallel',
  shards: 'multi-node',
}

/** The format chips, what the runtime supports, and whether its image is on
 *  the coordinator -- the only machine that can be asked. */
export function RuntimeFacts({ info }: { info: RuntimeInfo }) {
  return (
    <span className="mdl-rt-facts">
      {info.formats.map((f) => (
        <span key={f} className="pill">
          {f}
        </span>
      ))}
      {(Object.keys(SUPPORT_LABEL) as (keyof RuntimeInfo['supports'])[])
        .filter((k) => info.supports[k])
        .map((k) => (
          <span key={k} className="pill">
            {SUPPORT_LABEL[k]}
          </span>
        ))}
      <span className="unit">{info.image_present ? 'image on this machine' : 'image not pulled here'}</span>
    </span>
  )
}

// ── Speed ───────────────────────────────────────────────────────────────────

/** Search the published draft heads by repository or method and pick one,
 *  the same way `SpeculativeField` does. Scanned once, on first keystroke. */
export function DrafterSearch({ s }: { s: ModelSession }) {
  const { backend } = useBackend()
  const { route, navigate } = useRouter()
  const uid = useId()
  const [q, setQ] = useState('')
  const [heads, setHeads] = useState<SpeculativeHead[] | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const wanted = q.trim().length > 0
  useEffect(() => {
    if (!wanted || heads || err) return
    let live = true
    backend
      .speculativeHeads(s.subjectId)
      .then((r) => live && setHeads(r.heads))
      .catch((e: unknown) => live && setErr(errText(e)))
    return () => {
      live = false
    }
  }, [wanted, heads, err, backend, s.subjectId])

  const needle = q.trim().toLowerCase()
  const matches = (heads ?? []).filter(
    (h) => h.model_id.toLowerCase().includes(needle) || h.method.toLowerCase().includes(needle),
  )
  const rows = matches.slice(0, 6)
  const tail = s.subjectId.split('/').pop() ?? s.subjectId
  return (
    <div className="mdl-stack">
      <div className="fld">
        <label htmlFor={`${uid}-drafter`}>Find a draft head</label>
        <input
          id={`${uid}-drafter`}
          className="mono"
          type="search"
          spellCheck={false}
          placeholder="repository or method, e.g. eagle3"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
      </div>
      {err ? <Verbatim text={err} size="unit" /> : null}
      {wanted && heads == null && !err ? <p className="unit">Scanning the hub…</p> : null}
      {wanted && heads && !rows.length ? <p className="unit">No published head matches that.</p> : null}
      {rows.map((h) => {
        const inUse = route.spec?.model === h.model_id
        return (
          <div key={`${h.method}:${h.model_id}`} className="mdl-drafter">
            <span className="mono unit">{h.model_id}</span>
            <span className="pill">{h.method}</span>
            <span className="mono unit">{h.draft_bytes != null ? gib(h.draft_bytes) : '—'}</span>
            <button
              type="button"
              className="ghost"
              disabled={inUse}
              onClick={() => navigate({ spec: refFor(h) }, { replace: true })}
            >
              {inUse ? 'In use' : 'Use'}
            </button>
          </div>
        )
      })}
      {wanted && heads ? (
        <p className="unit">
          {`${matches.length} of ${heads.length} heads match · `}
          <a
            href={`https://huggingface.co/models?search=${encodeURIComponent(tail)}`}
            target="_blank"
            rel="noreferrer"
          >
            Search all of Hugging Face
          </a>
        </p>
      ) : null}
    </div>
  )
}

const BATCHED: SelectOption<string>[] = [
  { value: '', label: 'Engine default' },
  ...[2048, 4096, 8192, 16384, 32768].map((n) => ({ value: String(n), label: n.toLocaleString() })),
]

function batchedNote(n: number | null | undefined): string {
  if (!n) return 'The engine decides how many prompt tokens one scheduler step may read.'
  if (n <= 4096)
    return 'Small steps keep decode smooth while long prompts arrive, at the cost of reading each long prompt more slowly.'
  return 'Large steps read long prompts faster, and the activation memory the fit charges grows with them.'
}

export function SchedulerFields({ s }: { s: ModelSession }) {
  const uid = useId()
  const o = s.engineOptions
  return (
    <div className="mdl-stack">
      <div className="fld">
        <label htmlFor={`${uid}-batched`}>Tokens per scheduler step</label>
        <Select
          id={`${uid}-batched`}
          value={o.max_num_batched_tokens ? String(o.max_num_batched_tokens) : ''}
          options={BATCHED}
          onChange={(v) => s.setEngineOptions(withOption(o, { max_num_batched_tokens: v ? Number(v) : null }))}
        />
      </div>
      <p className="unit">{batchedNote(o.max_num_batched_tokens)}</p>
      <label className="mdl-check label">
        <input
          type="checkbox"
          checked={o.async_scheduling === true}
          onChange={(e) => s.setEngineOptions(withOption(o, { async_scheduling: e.target.checked }))}
        />
        Async scheduling <span className="mono unit">--async-scheduling</span>
      </label>
    </div>
  )
}

// ── Memory ──────────────────────────────────────────────────────────────────

const PREFIX: SelectOption<'' | 'on' | 'off'>[] = [
  { value: '', label: 'Engine default' },
  { value: 'on', label: 'On' },
  { value: 'off', label: 'Off' },
]

export function PrefixAndShare({ s }: { s: ModelSession }) {
  const uid = useId()
  const o = s.engineOptions
  const share = s.commandPreview?.gpu_memory_utilization
  return (
    <>
      <div className="fld">
        <label htmlFor={`${uid}-prefix`}>Prefix caching</label>
        <Select
          id={`${uid}-prefix`}
          value={o.prefix_caching == null ? '' : o.prefix_caching ? 'on' : 'off'}
          options={PREFIX}
          // `false` is a real value here (`--no-enable-prefix-caching`), so it
          // is written directly rather than through `withOption`.
          onChange={(v) => {
            const next = withOption(o, { prefix_caching: null })
            if (v) next.prefix_caching = v === 'on'
            s.setEngineOptions(next)
          }}
        />
      </div>
      <p className="unit">
        {`GPU share derate will request: ${share == null ? '—' : `${(share * 100).toFixed(1)}%`}`}
      </p>
      <p className="unit">
        Derived from what the verdict charges, capped by what is free. It is read off the command below, so it is
        the number the launch would send.
      </p>
    </>
  )
}

// ── Context: YaRN ───────────────────────────────────────────────────────────

export function YarnField({ s }: { s: ModelSession }) {
  const original = s.originalWindow ?? s.detail?.max_position_embeddings ?? null
  const factor = s.engineOptions.rope_factor ?? null
  if (!original || (s.liveContext <= original && !factor)) return null
  const want = Math.min(8, Math.ceil(s.liveContext / original))
  return (
    <div className="mdl-stack">
      <label className="mdl-check label">
        <input
          type="checkbox"
          checked={factor != null}
          onChange={(e) =>
            s.setEngineOptions(withOption(s.engineOptions, { rope_factor: e.target.checked ? Math.max(2, want) : null }))
          }
        />
        Stretch the window with YaRN
      </label>
      <p className="unit">
        {`This model was trained on ${original.toLocaleString()} tokens. YaRN stretches it ${factor ?? Math.max(2, want)}× ` +
          `to ${(original * (factor ?? Math.max(2, want))).toLocaleString()}; answers past the trained window are ` +
          'usually somewhat worse than inside it.'}
      </p>
    </div>
  )
}

// ── Multimodal ──────────────────────────────────────────────────────────────

export function MultimodalFields({ s }: { s: ModelSession }) {
  const uid = useId()
  const o = s.engineOptions
  if (!s.detail?.capabilities.vision.present) return <p className="unit">This model takes text only.</p>
  const images = o.limit_mm_per_prompt?.image
  return (
    <div className="mdl-stack">
      <div className="fld">
        <label htmlFor={`${uid}-imgs`}>{`Images per prompt: ${images ?? 'engine default'}`}</label>
        <input
          id={`${uid}-imgs`}
          type="range"
          min={0}
          max={16}
          value={images ?? 1}
          onChange={(e) =>
            s.setEngineOptions(withOption(o, { limit_mm_per_prompt: { ...o.limit_mm_per_prompt, image: Number(e.target.value) } }))
          }
        />
        {images != null ? (
          <button
            type="button"
            className="ghost"
            onClick={() => {
              const { image: _drop, ...rest } = o.limit_mm_per_prompt ?? {}
              s.setEngineOptions(withOption(o, { limit_mm_per_prompt: rest }))
            }}
          >
            Engine default
          </button>
        ) : null}
      </div>
      <div className="fld mdl-numfld">
        <label htmlFor={`${uid}-mmcache`}>Image cache (GiB)</label>
        <input
          id={`${uid}-mmcache`}
          className="mono"
          type="number"
          min={0}
          max={64}
          step={0.5}
          placeholder="default"
          value={o.mm_processor_cache_gb ?? ''}
          onChange={(e) =>
            s.setEngineOptions(
              withOption(o, { mm_processor_cache_gb: e.target.value === '' ? null : Number(e.target.value) }),
            )
          }
        />
      </div>
      <label className="mdl-check label">
        <input
          type="checkbox"
          checked={o.language_model_only === true}
          onChange={(e) => s.setEngineOptions(withOption(o, { language_model_only: e.target.checked }))}
        />
        Text only — skip loading the vision tower
      </label>
    </div>
  )
}

// ── API ─────────────────────────────────────────────────────────────────────

const SAMPLING: { key: 'temperature' | 'top_p' | 'top_k'; label: string; step: number }[] = [
  { key: 'temperature', label: 'temperature', step: 0.05 },
  { key: 'top_p', label: 'top_p', step: 0.05 },
  { key: 'top_k', label: 'top_k', step: 1 },
]

export function ApiFields({ s, info }: { s: ModelSession; info: RuntimeInfo | null }) {
  const { backend, invalidate } = useBackend()
  const uid = useId()
  const o = s.engineOptions
  const [minted, setMinted] = useState<{ secret: string; keyId: string } | null>(null)
  const [err, setErr] = useState<string | null>(null)
  /** Minting a key re-authenticates the signed-in admin. */
  const [currentPassword, setCurrentPassword] = useState('')
  const parserOptions = (names: string[]): SelectOption<string>[] => [
    { value: '', label: 'None' },
    ...names.map((n) => ({ value: n, label: n })),
  ]
  // `/v1` and the key allowlist name a model by its SERVED name, never its repo
  // id: the running deployment's, else the one the launch will default to
  // (deploy/sparkrun.py::default_served_name -- the repo's last segment).
  const name =
    s.servedName.trim() ||
    s.running?.served_name ||
    s.subjectId.split('/').pop()!.split(':')[0] ||
    s.subjectId
  // The longest expiry the server accepts below a year (KEY_EXPIRY_DAYS); any
  // value outside that list is a 400.
  const KEY_DAYS: (typeof KEY_EXPIRY_DAYS)[number] = 100
  const createKey = async () => {
    setErr(null)
    try {
      const r = await backend.createKey({
        name: `${name} key`,
        expiry_days: KEY_DAYS,
        allowed_models: [name],
        current_password: currentPassword,
      })
      setMinted({ secret: r.secret_once, keyId: r.key.key_id })
      setCurrentPassword('')
      invalidate()
    } catch (e) {
      setErr(errText(e))
    }
  }
  return (
    <div className="mdl-stack">
      {info?.reasoning_parsers.length ? (
        <div className="fld">
          <label htmlFor={`${uid}-reason`}>Thinking output</label>
          <Select
            id={`${uid}-reason`}
            value={o.reasoning_parser ?? ''}
            options={parserOptions(info.reasoning_parsers)}
            onChange={(v) => s.setEngineOptions(withOption(o, { reasoning_parser: v || null }))}
          />
        </div>
      ) : null}
      {info?.tool_call_parsers.length ? (
        <div className="fld">
          <label htmlFor={`${uid}-tools`}>Tool calls</label>
          <Select
            id={`${uid}-tools`}
            value={o.tool_call_parser ?? ''}
            options={parserOptions(info.tool_call_parsers)}
            onChange={(v) => s.setEngineOptions(withOption(o, { tool_call_parser: v || null }))}
          />
        </div>
      ) : null}
      <fieldset className="mdl-sampling">
        <legend className="label">Default sampling</legend>
        {SAMPLING.map((f) => (
          <div key={f.key} className="fld mdl-numfld">
            <label htmlFor={`${uid}-${f.key}`}>{f.label}</label>
            <input
              id={`${uid}-${f.key}`}
              className="mono"
              type="number"
              step={f.step}
              placeholder="model's own"
              value={o.generation_overrides?.[f.key] ?? ''}
              onChange={(e) => {
                const next = { ...o.generation_overrides }
                if (e.target.value === '') delete next[f.key]
                else next[f.key] = Number(e.target.value)
                s.setEngineOptions(withOption(o, { generation_overrides: next }))
              }}
            />
          </div>
        ))}
      </fieldset>
      <div className="fld">
        <label htmlFor={`${uid}-current-pw`}>Your password</label>
        <PasswordField
          id={`${uid}-current-pw`}
          value={currentPassword}
          autoComplete="current-password"
          onChange={setCurrentPassword}
        />
      </div>
      <div>
        <button type="button" className="ghost" disabled={!currentPassword} onClick={() => void createKey()}>
          Create a key for this model
        </button>
      </div>
      <p className="unit">{`A key allowed to call ${name} only, valid for ${KEY_DAYS} days. Its secret is shown once.`}</p>
      {err ? <Verbatim text={err} size="unit" /> : null}
      {minted ? <RevealModal secret={minted.secret} keyId={minted.keyId} onDone={() => setMinted(null)} /> : null}
    </div>
  )
}

// ── Lifecycle ───────────────────────────────────────────────────────────────

const IDLE: SelectOption<string>[] = [
  { value: '', label: 'Stay loaded' },
  { value: '900', label: 'Unload after 15 min' },
  { value: '3600', label: 'Unload after 1 h' },
  { value: '14400', label: 'Unload after 4 h' },
  { value: '86400', label: 'Unload after 24 h' },
]

const PRIORITY: { value: LifecyclePriority; label: string; note: string }[] = [
  { value: 'low', label: 'Low', note: 'Suggested first when something else needs the memory.' },
  { value: 'normal', label: 'Normal', note: 'Suggested in the ordinary order.' },
  { value: 'high', label: 'High', note: 'Suggested last.' },
  { value: 'pinned', label: 'Pinned', note: 'Never suggested.' },
]

export function LifecycleFields({ s }: { s: ModelSession }) {
  const { backend, invalidate } = useBackend()
  const uid = useId()
  const [err, setErr] = useState<string | null>(null)
  const running = s.running
  const cur: Lifecycle = {
    idle_unload_s: null,
    priority: 'normal',
    start_at_boot: false,
    ...(running?.lifecycle ?? s.lifecycle ?? {}),
  }
  // A running deployment changes now; otherwise this rides the launch.
  const set = async (patch: Partial<Lifecycle>) => {
    setErr(null)
    if (!running) {
      s.setLifecycle({ ...s.lifecycle, ...patch })
      return
    }
    try {
      await backend.setDeploymentLifecycle(running.deployment_id, patch)
      invalidate()
    } catch (e) {
      setErr(errText(e))
    }
  }
  const note = PRIORITY.find((p) => p.value === cur.priority)?.note ?? ''
  return (
    <div className="mdl-stack">
      {running ? <p className="unit">{`Applies to the running ${running.served_name} now.`}</p> : null}
      <div className="fld">
        <label htmlFor={`${uid}-idle`}>When idle</label>
        <Select
          id={`${uid}-idle`}
          value={cur.idle_unload_s ? String(cur.idle_unload_s) : ''}
          options={IDLE}
          onChange={(v) => void set({ idle_unload_s: v ? Number(v) : null })}
        />
      </div>
      <div className="fld">
        <label htmlFor={`${uid}-prio`}>Priority</label>
        <Select
          id={`${uid}-prio`}
          value={cur.priority}
          options={PRIORITY.map(({ value, label }) => ({ value, label }))}
          onChange={(v) => void set({ priority: v })}
        />
      </div>
      <p className="unit">{`${note} derate only uses this to order its stop suggestions.`}</p>
      <label className="mdl-check label">
        <input
          type="checkbox"
          checked={cur.start_at_boot}
          onChange={(e) => void set({ start_at_boot: e.target.checked })}
        />
        Start when the cluster boots
      </label>
      {err ? <Verbatim text={err} size="unit" /> : null}
    </div>
  )
}

// ── Custom flags: env rows and hints ────────────────────────────────────────

/** Flags worth a click, each one `_EXTRA_ARG_SAFE` accepts. */
const FLAG_HINTS: { k: string; v: string }[] = [
  { k: '--max-num-seqs', v: '64' },
  { k: '--seed', v: '0' },
  { k: '--max-logprobs', v: '20' },
  { k: '--disable-log-requests', v: '' },
  { k: '--swap-space', v: '4' },
]

/** Hints are vLLM's own flag names; `engines/flagcatalog.py` is not exposed
 *  per runtime, so other engines get none rather than a guess. */
export function FlagHints({ s }: { s: ModelSession }) {
  if (s.runtime !== 'vllm') return null
  const present = new Set(s.flags.map((f) => f.k.trim()))
  const offer = FLAG_HINTS.filter((h) => !present.has(h.k))
  if (!offer.length) return null
  return (
    <span className="chips" role="group" aria-label="Common flags">
      {offer.map((h) => (
        <button key={h.k} type="button" onClick={() => s.setFlags([...s.flags, { ...h }])}>
          {h.k}
        </button>
      ))}
    </span>
  )
}

const ENV_HINTS = ['NCCL_SOCKET_IFNAME', 'NCCL_DEBUG', 'NCCL_IB_HCA', 'NCCL_MAX_NCHANNELS']

export function EnvRows({ s, tunables }: { s: ModelSession; tunables: string[] }) {
  const options: SelectOption<string>[] = [
    { value: '', label: 'Choose a variable' },
    ...tunables.map((t) => ({ value: t, label: t })),
  ]
  const edit = (i: number, patch: { k?: string; v?: string }) =>
    s.setEnvRows(s.envRows.map((r, j) => (j === i ? { ...r, ...patch } : r)))
  const present = new Set(s.envRows.map((r) => r.k))
  const envHints = ENV_HINTS.filter((k) => tunables.includes(k) && !present.has(k))
  return (
    <div className="mdl-stack">
      <span className="label">Environment variables</span>
      {envHints.length ? (
        <span className="chips" role="group" aria-label="Common NCCL variables">
          {envHints.map((k) => (
            <button key={k} type="button" onClick={() => s.setEnvRows([...s.envRows, { k, v: '' }])}>
              {k}
            </button>
          ))}
        </span>
      ) : null}
      {s.envRows.map((r, i) => (
        <div key={i} className="mdl-flag">
          <Select aria-label={`Variable ${i + 1}`} value={r.k} options={options} onChange={(k) => edit(i, { k })} />
          <input
            className="mono"
            type="text"
            aria-label={`Value of variable ${i + 1}`}
            spellCheck={false}
            value={r.v}
            onChange={(e) => edit(i, { v: e.target.value })}
          />
          <button type="button" className="ghost" onClick={() => s.setEnvRows(s.envRows.filter((_, j) => j !== i))}>
            Remove
          </button>
        </div>
      ))}
      <div>
        <button type="button" className="ghost" onClick={() => s.setEnvRows([...s.envRows, { k: '', v: '' }])}>
          Add variable
        </button>
      </div>
      <p className="unit">
        NCCL tuning only: these are the variables this image’s NCCL library reads, and a single-machine launch has no
        communicator to tune.
      </p>
    </div>
  )
}

// ── Command preview ─────────────────────────────────────────────────────────

export function CommandPreviewBlock({ s }: { s: ModelSession }) {
  const { setWantPreview } = s
  useEffect(() => {
    setWantPreview(true)
    return () => setWantPreview(false)
  }, [setWantPreview])
  const nodes = s.result?.plan.node_ids ?? []
  const p = s.commandPreview
  return (
    <section className="mdl-preview" aria-label="Command preview">
      <h3 className="label">{`Command derate will run on ${nodes.length ? nodes.join(', ') : 'the planned machines'}`}</h3>
      {s.commandPreviewError ? (
        <Verbatim text={s.commandPreviewError} size="unit" />
      ) : p ? (
        <>
          <Copyable text={p.argv.join(' \\\n  ')} />
          <details>
            <summary className="unit">Recipe</summary>
            <pre className="mono mdl-recipe">{p.recipe}</pre>
          </details>
          {p.notes.map((n) => (
            <Verbatim key={n} text={n} size="unit" />
          ))}
        </>
      ) : (
        <p className="unit">{s.result ? 'Rendering…' : 'Shown once there is a verdict.'}</p>
      )}
    </section>
  )
}
