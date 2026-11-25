import { useState } from 'react'
import type {
  LaunchRequest,
  Profile,
  Provider,
  ProviderCatalogueModel,
  ProviderKindSpec,
} from '../../api/types'
import { Icon } from '../../components/Icon'
import { Lamp } from '../../components/Lamp'
import { Verbatim } from '../../components/Verbatim'
import { useBackend } from '../../state/backend'
import { AdvancedTab } from './AdvancedTab'
import { BackendPicker } from './BackendPicker'
import { CapabilityChips } from './CapabilityChips'
import { HistoryTab } from './HistoryTab'
import { HubTab } from './HubTab'
import { FailureBanner, LivePanel } from './LivePanel'
import { OwnerMark, useAvatars } from './OwnerMark'
import { ownerOf } from './owner'
import { QuantSection } from './QuantSection'
import { ServeActions } from './ServeActions'
import { SizingSliders } from './SizingSliders'
import { UsersChart } from './UsersChart'
import { RouteCard } from './RouteCard'
import type { ModelRow, RowProvider } from './rows'
import { useModelSession } from './useModelSession'
import { VerdictBox } from './VerdictBox'
import { formatParams, gib } from './verdictView'
import { BenchmarksTab } from './BenchmarksTab'
import { ProfilesTab } from './ProfilesTab'

type Pane = 'serve' | 'advanced' | 'profiles' | 'benchmarks' | 'history' | 'hub'

const PANES: { id: Pane; label: string }[] = [
  { id: 'serve', label: 'Serve' },
  { id: 'advanced', label: 'Advanced' },
  { id: 'profiles', label: 'Profiles' },
  { id: 'benchmarks', label: 'Benchmarks' },
  { id: 'history', label: 'History' },
  { id: 'hub', label: 'Hugging Face' },
]

/** One model: whether it runs here, on which machines, as which file, and how.
 *
 *  Every request this pane makes is owned by `useModelSession`, so the verdict
 *  in the box, the Serve button beside it and the launch it sends are one
 *  value rather than three that could drift. Mounted keyed on the model id by
 *  both callers (the Models tab's split and the shell's sheet), so a different
 *  model always gets a fresh session. */
export function ModelPane({
  modelId,
  context,
  concurrency,
  providers,
  providerKinds,
  catalogues,
  catalogueErrors,
  preferRoute = false,
  initialCustomCommand,
  preferQuant,
  initialRequest,
  row,
  onClose,
}: {
  modelId: string
  /** Null on the default path: the coordinator chooses one per variant out of
   *  what actually fits. Already debounced by the caller. */
  context: number | null
  concurrency: number | null
  /** Polled by the screen that owns this pane and handed down, so the pane
   *  and its siblings render one value. */
  providers?: Provider[] | null
  providerKinds?: ProviderKindSpec[] | null
  /** Each provider's WHOLE catalogue, keyed by provider id. */
  catalogues?: Record<string, ProviderCatalogueModel[]> | null
  catalogueErrors?: { provider_id: string; message: string }[] | null
  /** Open on the routing side rather than the launcher, for a row that exists
   *  only because a provider publishes it. Read once, at mount. */
  preferRoute?: boolean
  /** Seeds the custom-command field from a reused custom serve. Read once. */
  initialCustomCommand?: string
  /** A quantization named on the way in, by a pasted `hf.co/<repo>:<QUANT>`. */
  preferQuant?: string | null
  /** A saved setup to apply once the ladder lands (the Profiles view's Load). */
  initialRequest?: { request: LaunchRequest; profile: Profile | null }
  /** The list's row for this model, when the caller has one: the hub facts
   *  (downloads, likes, gated) live there and nowhere on the detail payload. */
  row?: ModelRow | null
  onClose: () => void
}) {
  // Once for the whole pane: OwnerMark never subscribes for itself.
  useAvatars()
  const [pane, setPane] = useState<Pane>('serve')
  const s = useModelSession({
    modelId,
    context,
    concurrency,
    providers,
    providerKinds,
    catalogues,
    preferRoute,
    initialCustomCommand,
    preferQuant,
    initialRequest,
  })
  const { detail, variant, running } = s

  const params = row?.total_params ?? detail?.shape.total_params ?? null
  const meta = [
    ownerOf(modelId),
    params ? formatParams(params) : null,
    row?.downloads != null ? `${row.downloads.toLocaleString()} downloads` : null,
    s.cache.nodes(modelId).length ? 'on disk' : 'not downloaded',
  ]
    .filter(Boolean)
    .join(' · ')

  const noLocalVerdict = s.detailError != null && s.servedBy.length > 0
  const showVerdict = (pane === 'serve' || pane === 'advanced') && !noLocalVerdict

  // What the footer describes: the deployment while one runs, otherwise the
  // launch the verdict box was taken for -- including WHERE, because the same
  // numbers on a different machine are a different launch.
  const seqs = (n: number | null | undefined) =>
    n == null ? '— sequences' : `${n} ${n === 1 ? 'sequence' : 'sequences'}`
  const on = (ids: readonly string[] | null | undefined) =>
    ids && ids.length ? `on ${ids.join(', ')}` : null
  const summary = (
    running
      ? [
          running.runtime,
          on(running.node_ids),
          `${running.context_length.toLocaleString()} context`,
          seqs(running.max_concurrent_seqs),
          running.kv_dtype ? `kv ${running.kv_dtype}` : null,
          running.served_name,
        ]
      : [
          s.runtime,
          on(s.result?.plan?.node_ids),
          s.addingCopy && !s.servedName.trim() && s.copyName ? `as ${s.copyName}` : null,
          `${s.result?.context?.toLocaleString() ?? '—'} context`,
          seqs(s.result?.concurrency),
          s.kvDtype ? `kv ${s.kvDtype}` : null,
          variant && s.cache.complete(variant.repo_id, variant.file_bytes) !== true
            ? `downloads ${gib(variant.file_bytes)} from ${variant.repo_id}`
            : null,
        ]
  )
    .filter(Boolean)
    .join(' · ')
  const profileTag = s.activeProfile ? `Profile: ${s.activeProfile.name}${s.modified ? ' (changed)' : ''} · ` : ''
  const { backend, invalidate } = useBackend()
  const [saveError, setSaveError] = useState<string | null>(null)
  // The Profiles tab's "Save current settings", from the footer.
  const saveProfile = () => {
    const name = window.prompt('Name this setup', `Saved setup ${s.profiles.length + 1}`)?.trim()
    if (!name) return
    backend
      .createProfile({ model_id: modelId, name, request: s.currentRequest() })
      .then((p) => {
        s.markSaved(p)
        setSaveError(null)
        invalidate()
      })
      .catch((e: unknown) => setSaveError(e instanceof Error ? e.message : String(e)))
  }
  return (
    <div className="mdl-detail">
      <header className="mdl-head">
        <OwnerMark owner={ownerOf(modelId)} variant="card" />
        <div className="mdl-head-text">
          <h3 className="mono mdl-head-id">{modelId}</h3>
          <span className="unit">{meta}</span>
          {detail ? <CapabilityChips detail={detail} /> : null}
        </div>
        <button type="button" className="ghost" onClick={onClose} aria-label="Close">
          <Icon name="close" size={14} decorative />
        </button>
      </header>

      {/* Where serving would happen. Only drawn when there is a second answer:
          with no provider publishing this model, "This cluster" is not a
          choice and a control offering one option is furniture. */}
      {s.routeTargets.length ? (
        <div className="mdl-seg mdl-dest" role="group" aria-label="Where to serve this model">
          <button
            type="button"
            aria-pressed={s.destination === 'cluster'}
            onClick={() => s.setDestination('cluster')}
          >
            This cluster
          </button>
          {s.routeTargets.map((t) => (
            <button
              key={t.provider_id}
              type="button"
              aria-pressed={s.destination === t.provider_id}
              onClick={() => s.setDestination(t.provider_id)}
            >
              Route to {t.display_name}
              {t.served ? <Icon name="check" size={12} label="served" className="icon-inline" /> : null}
            </button>
          ))}
        </div>
      ) : null}

      <div className="subs" role="tablist" aria-label="Model sections">
        {PANES.map((p) => (
          <button
            type="button"
            key={p.id}
            role="tab"
            aria-selected={pane === p.id}
            onClick={() => setPane(p.id)}
          >
            {p.label}
          </button>
        ))}
      </div>

      {showVerdict ? <VerdictBox s={s} onAdvancedTab={() => setPane('advanced')} /> : null}

      {pane === 'serve' ? (
        s.routing ? (
          // Routing is the whole tab: there is no plan to run, no machine to
          // pick and no quantization to choose for a model on somebody else's
          // hardware.
          <>
            <RouteCard
              facts={s.routing}
              catalogue={catalogues?.[s.routing.provider_id] ?? null}
              catalogueError={
                catalogueErrors?.find((f) => f.provider_id === s.routing!.provider_id)?.message ?? null
              }
            />
            {/* Self-hiding: a provider kind that does not aggregate backend
                hosts per model answers 400, and this renders nothing. */}
            <BackendPicker
              providerId={s.routing.provider_id}
              upstreamId={s.routing.upstream_id}
              fallback={
                catalogues?.[s.routing.provider_id]?.find((m) => m.upstream_id === s.routing!.upstream_id)
                  ?.backend_fallback ?? false
              }
            />
          </>
        ) : noLocalVerdict ? (
          <>
            <ProviderFacts rows={s.servedBy} />
            {/* No local answer, and there never will be one: the id is not a
                repository anything here can resolve. The resolver's own
                sentence says why -- on a gated repo, to set HF_TOKEN. */}
            <div className="sub">no local verdict</div>
            <Verbatim text={s.detailError!} size="unit" />
          </>
        ) : (
          <>
            {s.servedBy.length ? <ProviderFacts rows={s.servedBy} /> : null}
            {s.lastFailure ? <FailureBanner dep={s.lastFailure} onDetails={() => setPane('history')} /> : null}
            {running ? <LivePanel dep={running} /> : null}
            <QuantSection s={s} />
            <SizingSliders s={s} />
            <UsersChart s={s} />
          </>
        )
      ) : pane === 'advanced' ? (
        <AdvancedTab s={s} />
      ) : pane === 'profiles' ? (
        <ProfilesTab
          s={s}
          modelId={modelId}
          onAdvancedTab={() => setPane('advanced')}
          onServeTab={() => setPane('serve')}
        />
      ) : pane === 'benchmarks' ? (
        <BenchmarksTab s={s} modelId={modelId} />
      ) : pane === 'history' ? (
        <HistoryTab s={s} onServeTab={() => setPane('serve')} />
      ) : (
        <HubTab s={s} modelId={modelId} row={row ?? null} onServeTab={() => setPane('serve')} />
      )}

      {!s.routing && !noLocalVerdict ? (
        <footer className="mdl-foot">
          {running ? (
            <button
              type="button"
              className="ghost"
              onClick={() => {
                if (window.confirm(`Stop ${running.served_name} on ${running.node_ids.join(", ")}? Requests to it fail until it is served again.`)) {
                  void s.stop(running.deployment_id)
                }
              }}
            >
              Stop
            </button>
          ) : null}
          {s.live.length && !s.addingCopy && pane === 'serve' ? (
            <button type="button" className="ghost" onClick={s.startCopy}>
              Serve another copy
            </button>
          ) : null}
          {s.addingCopy ? (
            <button type="button" className="ghost" onClick={s.cancelCopy}>
              Cancel copy
            </button>
          ) : null}
          {pane === 'serve' ? <ServeActions s={s} /> : null}
          {/* The Profiles pane has its own "Save current settings" at the top. */}
          {!running && pane !== 'profiles' ? (
            <button type="button" className="ghost" onClick={saveProfile}>
              Save as profile
            </button>
          ) : null}
          {saveError ? <Verbatim text={saveError} size="unit" /> : null}
          <span className="unit mdl-foot-summary">
            {profileTag}
            {summary}
          </span>
        </footer>
      ) : null}
    </div>
  )
}

/** Who else serves this model, and on what terms.
 *
 *  Every figure is read off `/api/providers` and none is computed. A null
 *  price prints as "not priced" and never as $0: the wire distinguishes
 *  "never published a price" from "free". Nothing key-shaped is rendered. */
function ProviderFacts({ rows }: { rows: RowProvider[] }) {
  const price = (v: number | null) => (v == null ? null : `$${v.toFixed(2)}`)
  return (
    <>
      <div className="sub">served elsewhere</div>
      {rows.map((p) => {
        const inp = price(p.input_cost_per_mtok)
        const out = price(p.output_cost_per_mtok)
        return (
          <div key={`${p.provider_id}::${p.served_name}`} className="mdl-provider">
            <div className="mdl-provider-line">
              <Lamp signal={p.healthy ? 'live' : 'fault'} label={p.healthy ? 'healthy' : 'unhealthy'} />
              <span className="mdl-provider-name">{p.display_name}</span>
              {/* The string you would actually send as `model` at /v1. */}
              <span className="mono unit">{p.served_name}</span>
            </div>
            <div className="unit mdl-provider-sub">
              {[
                p.context_length ? `${p.context_length.toLocaleString()} context` : null,
                inp && out ? `${inp} / Mtok in · ${out} / Mtok out` : 'not priced',
                p.supports_streaming ? 'streaming' : null,
                p.supports_tools ? 'tools' : null,
              ]
                .filter(Boolean)
                .join(' · ')}
            </div>
            {p.last_error ? (
              <div className="mdl-provider-sub">
                <Verbatim text={p.last_error} size="unit" />
              </div>
            ) : null}
            {p.admitting === false && p.admission_block ? (
              <div className="mdl-provider-sub">
                <Verbatim text={p.admission_block} size="unit" />
              </div>
            ) : null}
          </div>
        )
      })}
    </>
  )
}
