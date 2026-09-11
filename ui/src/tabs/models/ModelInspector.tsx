import { useEffect, useMemo, useRef, useState } from 'react'
import type {
  ModelDetail,
  Provider,
  ProviderCatalogueModel,
  ProviderKindSpec,
  VariantLadder,
} from '../../api/types'
import { ENDPOINT_FOR_MODALITY, isAudio } from '../../api/types'
import type { Runtime } from '../../state/runtime'
import { runtimeFor } from '../../state/runtime'
import { useBackend } from '../../state/backend'
import { usePlacement } from '../../state/placement'
import { useCluster, useStorage } from '../../state/resources'
import { Lamp } from '../../components/Lamp'
import { Verbatim, VerbatimList } from '../../components/Verbatim'
import { CapabilityChips } from './CapabilityChips'
import { pullableProviders } from './pullTargets'
import { ParamBreakdownTable } from './ParamBreakdownTable'
import { BackendPicker } from './BackendPicker'
import { RouteCard, type RouteTargetFacts } from './RouteCard'
import { ServePanel } from './ServePanel'
import { cacheIndex, providerRowsFor, type RowProvider } from './rows'
import { gbytes } from '../../format'

/** One model: where it would run and what you can get it as, with what it is
 *  one tab away.
 *
 *  Fetches its own two payloads rather than receiving them, because both are
 *  hub-bound and neither belongs in `resources.ts` -- `useResource` refires on
 *  every `revision` bump, so an open model would re-run its hub calls after
 *  every launch, admit and settings change.
 *
 *  The two loads are deliberately separate and shown separately. Detail is one
 *  resolve and usually cached; the ladder is a search plus a repository read
 *  per GGUF repo and can take seconds. Waiting for the slow one before drawing
 *  the fast one would leave the pane blank for no reason. */
/** One line per distinct reason, naming the machines that were not asked.
 *
 *  Grouped rather than listed per node: today every skip shares one reason, and
 *  three identical sentences under a heading read as three problems. The server
 *  owns the wording -- this only decides how many times to say it. */
function skippedLines(
  skipped: { node_id: string; reason: string }[] | undefined,
): string[] {
  if (!skipped?.length) return []
  const byReason = new Map<string, string[]>()
  for (const s of skipped) {
    const ids = byReason.get(s.reason) ?? []
    ids.push(s.node_id)
    byReason.set(s.reason, ids)
  }
  return [...byReason].map(([reason, ids]) => {
    const noun = ids.length === 1 ? 'node' : 'nodes'
    return `${ids.length} ${noun} not considered: ${ids.join(', ')} — ${reason}`
  })
}

type Pane = 'serve' | 'about'

const PANES: { id: Pane; label: string }[] = [
  { id: 'serve', label: 'Serve' },
  { id: 'about', label: 'About' },
]

export function ModelInspector({
  modelId,
  context,
  concurrency,
  providers,
  providerKinds,
  catalogues,
  catalogueErrors,
  preferRoute = false,
  initialCustomCommand,
  onClose,
}: {
  modelId: string
  /** Null on the default path: nobody named a context, so the coordinator
   *  chooses one per variant out of what actually fits. Only non-null once
   *  somebody opens the Serve panel's advanced disclosure, or arrives on a
   *  link that already carried an override. */
  context: number | null
  concurrency: number | null
  /** Polled by the screen that owns this pane -- `useResource` does not
   *  deduplicate, so mounting `useProviders()` here would fan a second poll
   *  for a list already in memory. */
  providers?: Provider[] | null
  /** The server's kind table, polled alongside `providers`. Together they say
   *  which providers can be told to fetch weights. */
  providerKinds?: ProviderKindSpec[] | null
  /** Each provider's WHOLE catalogue, keyed by provider id. Polled by the tab.
   *
   *  Two jobs, and the second is the load-bearing one: it says which providers
   *  publish this model without serving it, and it carries every OTHER model's
   *  current state -- which the allowlist write needs, because that write is
   *  the complete set and not a delta. */
  catalogues?: Record<string, ProviderCatalogueModel[]> | null
  catalogueErrors?: { provider_id: string; message: string }[] | null
  /** Open on the routing side rather than the launcher.
   *
   *  Set by the list for a row whose only reason to exist is that a provider
   *  publishes it: there is nothing here to run, so "Run it here" would be a
   *  strange thing to land on. Read once, at mount -- the pane is keyed on the
   *  model id, so every model gets its own first answer and nothing yanks the
   *  choice out from under somebody mid-read. */
  preferRoute?: boolean
  /** Passed straight through to `ServePanel`'s field of the same name --
   *  see there. Owned by the list, not this pane, because it names a choice
   *  ("reuse this custom serve") made before the pane opened. */
  initialCustomCommand?: string
  onClose: () => void
}) {
  const { backend } = useBackend()
  // The machines ticked on the board below, which are part of the question the
  // ladder answers. Joined into a string for the effect's dependency list: the
  // array identity changes on every render even when the ids have not, and a
  // fresh 20s hub enumeration per render is not a refetch, it is a loop.
  const { nodeIds } = usePlacement()
  const nodeKey = (nodeIds ?? []).join(',')
  const [detail, setDetail] = useState<ModelDetail | null>(null)
  const [detailError, setDetailError] = useState<string | null>(null)
  const [ladder, setLadder] = useState<VariantLadder | null>(null)
  const [ladderError, setLadderError] = useState<string | null>(null)
  const [loadingLadder, setLoadingLadder] = useState(true)

  // Which half of the pane is showing. Component state and not a query
  // parameter: the URL names the screen and what is selected on it, and which
  // of two views of one subject you are reading is neither. The dashboard's
  // sub-tabs are held the same way, for the same reason.
  const [pane, setPane] = useState<Pane>('serve')

  // Owned here rather than inside the ladder, because the plan call and the
  // launch have to agree about them: `target` changes what the planner ranks,
  // and two copies of it would let the verdict describe one shape while the
  // Serve button starts another.
  const [target, setTarget] = useState<'throughput' | 'latency'>('throughput')
  const [runtime, setRuntime] = useState<Runtime>('vllm')
  // Why the runtime above is what it is, when the answer was not the default.
  // Rendered next to the picker rather than inferred from it: a control that
  // moved on its own has to say what moved it, or it reads as a bug.
  const [runtimeBecause, setRuntimeBecause] = useState<string | null>(null)
  // The provider a pull would land on, for the same reason `runtime` is here:
  // the ladder sends it and the panel picks it, and two copies would let the
  // machine named on screen differ from the one that gets the download. Empty
  // means "the first configured target", resolved at the point of use rather
  // than seeded, so a provider appearing after this mounted is still offered.
  const [providerId, setProviderId] = useState('')

  const pullTargets = useMemo(
    () => pullableProviders(providers ?? undefined, providerKinds ?? undefined),
    [providers, providerKinds],
  )
  // Resolved rather than seeded: `providerId` stays empty until somebody
  // picks, so the first configured target is the default and a provider added
  // while this pane is open becomes selectable without a remount. A stale id
  // -- the provider was removed -- falls back the same way instead of
  // addressing a machine that is no longer there.
  const chosenProvider =
    pullTargets.find((p) => p.provider_id === providerId)?.provider_id ??
    pullTargets[0]?.provider_id ??
    ''

  // Polled once here and handed to both halves. `useResource` does not
  // deduplicate -- every call is its own interval and its own request -- so
  // mounting `useStorage` in the panel and again in the ladder would fan two
  // cluster-wide disk walks out every 30s for one answer.
  const storage = useStorage(true)
  const cluster = useCluster()
  const cache = useMemo(() => cacheIndex(storage.data), [storage.data])

  /** Every provider that publishes this exact id. Joined here rather than
   *  received, so the pane is self-sufficient from a cold URL -- somebody
   *  reloading `/models/openai/gpt-4o` gets the answer without the list having
   *  produced a row for it first. */
  const servedBy = useMemo(
    () => providerRowsFor(providers, modelId),
    [providers, modelId],
  )

  /** Every provider that could carry this model at `/v1`, served or not.
   *
   *  Assembled from both provider surfaces because neither has all of it. The
   *  filtered listing knows health and admission for what is already served;
   *  the catalogue knows what is merely published. A provider in both -- the
   *  two endpoints poll on different intervals, so that happens for a tick
   *  after a switch -- is counted once, on the served side, which is the
   *  stronger fact. */
  const routeTargets = useMemo<RouteTargetFacts[]>(() => {
    const chosen = new Map(
      (providers ?? []).map((p) => [p.provider_id, p.models_chosen ?? null]),
    )
    const out: RouteTargetFacts[] = servedBy.map((p) => ({
      provider_id: p.provider_id,
      display_name: p.display_name,
      served_name: p.served_name,
      // `providerRows` keys its rows on the upstream id, so for a served
      // model that is this pane's own id.
      upstream_id: modelId,
      context_length: p.context_length,
      input_cost_per_mtok: p.input_cost_per_mtok,
      output_cost_per_mtok: p.output_cost_per_mtok,
      supports_tools: p.supports_tools,
      supports_streaming: p.supports_streaming,
      served: true,
      healthy: p.healthy,
      admission_block: p.admission_block,
      models_chosen: chosen.get(p.provider_id) ?? null,
    }))
    const seen = new Set(out.map((t) => t.provider_id))
    for (const [providerId, rows] of Object.entries(catalogues ?? {})) {
      if (seen.has(providerId)) continue
      const m = (rows ?? []).find((r) => r.upstream_id === modelId && !r.enabled)
      if (!m) continue
      out.push({
        provider_id: providerId,
        display_name:
          (providers ?? []).find((p) => p.provider_id === providerId)?.display_name ||
          providerId,
        served_name: m.served_name,
        upstream_id: m.upstream_id,
        context_length: m.context_length ?? null,
        input_cost_per_mtok: m.input_cost_per_mtok,
        output_cost_per_mtok: m.output_cost_per_mtok,
        supports_tools: m.supports_tools,
        supports_streaming: m.supports_streaming,
        served: false,
        // Nothing on this endpoint says whether the provider is up, and a
        // guess would draw a red lamp beside a healthy provider.
        healthy: null,
        admission_block: null,
        models_chosen: chosen.get(providerId) ?? null,
      })
    }
    return out
  }, [servedBy, catalogues, providers, modelId])

  /** Where serving this model would happen: on the cluster, or by routing.
   *
   *  One control for two verbs, because from the operator's side they answer
   *  one question. `'cluster'` is the launcher -- plan, fit gate, sparkrun --
   *  and a provider id is the allowlist, where nothing is launched at all and
   *  the only thing that changes is whether this cluster's `/v1` carries the
   *  name.
   *
   *  Seeded once. The pane is keyed on the model id, so a different model gets
   *  a fresh answer, and switching by hand is never undone by a poll landing. */
  const [destination, setDestination] = useState<string>(
    () => (preferRoute ? routeTargets[0]?.provider_id : null) ?? 'cluster',
  )
  // A provider that has gone away -- removed, or its catalogue emptied -- must
  // not leave the pane addressing it. Falls back to the launcher rather than
  // to another provider: which provider was a choice, and picking a different
  // one on somebody's behalf is a decision this screen does not get to make.
  const routing =
    routeTargets.find((t) => t.provider_id === destination) ?? null

  // One sequence per mount, bumped on every model change, so a response for a
  // model the pane no longer shows cannot land -- the same reason the plan
  // effect in `ServePanel` bumps on its early-return path too.
  const seq = useRef(0)

  useEffect(() => {
    const mine = ++seq.current
    setDetail(null)
    setDetailError(null)
    setLadder(null)
    setLadderError(null)
    setLoadingLadder(true)

    const enumerate = () => {
      backend
        // The ticked machines go with the question. Without them the server
        // sized every row on one machine while the Serve button below launched
        // onto several, and the pane apologised for the gap in prose instead
        // of closing it.
        .modelVariants(modelId, { context, concurrency, nodeIds })
        .then((l) => {
          if (seq.current === mine) setLadder(l)
        })
        .catch((e: unknown) => {
          if (seq.current === mine) {
            setLadderError(e instanceof Error ? e.message : String(e))
          }
        })
        .finally(() => {
          if (seq.current === mine) setLoadingLadder(false)
        })
    }

    // `modelDetail` is the authority on whether this id resolves at all.
    const resolving = backend
      .modelDetail(modelId)
      .then((d) => {
        if (seq.current === mine) {
          setDetail(d)
          // The runtime follows the model, once, when the model changes. A
          // speech checkpoint cannot load on vllm at all -- it is not slower
          // there, it is refused -- so leaving the picker on the default
          // would greet a TTS model with a red verdict and no hint that one
          // control away is a runtime that runs it. Set on arrival rather
          // than derived at render, so it stays a *default*: picking vllm to
          // read its refusal is a legitimate thing to do and this must not
          // undo it on the next poll.
          //
          // The cluster is the second input, and it is a recommendation
          // rather than a requirement: on a box with no GPU every CUDA
          // runtime is refused on every machine, so the same argument applies
          // with the same remedy. `runtimeFor` returns the reason with the
          // choice, and the reason is what goes on screen -- a picker that
          // moved on its own and says nothing reads as a bug.
          //
          // Deliberately inside the same `modelDetail` resolve rather than an
          // effect of its own: the roster refreshes on a timer, and rerunning
          // this when it does would overwrite a runtime somebody had picked.
          const pick = runtimeFor(d.modality, cluster.data?.nodes)
          setRuntime(pick.runtime)
          setRuntimeBecause(pick.because)
        }
        return true
      })
      .catch((e: unknown) => {
        if (seq.current === mine) {
          setDetailError(e instanceof Error ? e.message : String(e))
        }
        return false
      })

    if (!servedBy.length) {
      // The ordinary case, and the two calls stay parallel for the reason the
      // docstring above gives: detail is one cached resolve, the ladder is a
      // search plus a repository read apiece.
      enumerate()
    } else {
      // A model somebody's provider serves may not exist on the hub at all --
      // `openai/gpt-4o` does not. Firing the ladder for it is a guaranteed
      // second failure, and its 20s timeout would sit there spinning under a
      // pane that already knows the answer. So wait for the one call that can
      // tell us, and only enumerate if the id turned out to be real.
      //
      // The cost is that a provider-served model that IS on the hub waits for
      // a usually-cached resolve before its ladder starts. Worth it.
      resolving.then((ok) => {
        if (seq.current !== mine) return
        if (ok) enumerate()
        else setLoadingLadder(false)
      })
    }
  }, [backend, modelId, context, concurrency, nodeKey, servedBy.length])

  return (
    <>
      <div
        style={{
          display: 'flex',
          alignItems: 'baseline',
          gap: 10,
          marginBottom: 4,
        }}
      >
        <h3 className="mono" style={{ flex: 1, wordBreak: 'break-all' }}>
          {modelId}
        </h3>
        <button className="ghost" onClick={onClose} aria-label="Close">
          Close
        </button>
      </div>

      {/* What it is, in one line. Kept above the tabs because it is the
          caption for both of them. */}
      {detail ? (
        <div className="unit" style={{ marginBottom: 10 }}>
          {detail.architectures.join(', ') || detail.model_type || 'architecture unknown'}
          {/* The route, and only when it is not the one everything else
              answers on. A text model saying `POST /v1/chat/completions`
              here would be noise on every model in the catalogue; a speech
              model NOT saying `/v1/audio/speech` is the one thing a reader
              cannot guess -- they will point a chat client at it and get a
              400 that names a modality they never chose. */}
          {isAudio(detail.modality) ? (
            <>
              {' · '}
              <span className="mono">POST {ENDPOINT_FOR_MODALITY[detail.modality!]}</span>
            </>
          ) : null}
          {detail.from_cache ? ' · from cache' : null}
        </div>
      ) : detailError ? null : (
        <p className="unit">Resolving…</p>
      )}

      {/* Serving is why the pane is open. The reference sections used to sit
          between picking a model and the only control on the screen -- six of
          them, parameters through assumptions -- and reading them is
          occasional. They are one click away rather than in the way. */}
      <div className="subs" role="tablist" aria-label="Model sections">
        {PANES.map((p) => (
          <button
            key={p.id}
            role="tab"
            aria-selected={pane === p.id}
            onClick={() => setPane(p.id)}
          >
            {p.label}
          </button>
        ))}
      </div>

      <div hidden={pane !== 'serve'}>
        {/* Where serving would happen. Only drawn when there is a second
            answer: with no provider publishing this model, "Run it here" is
            not a choice and a control offering one option is furniture. */}
        {routeTargets.length ? (
          <div
            className="chips"
            role="group"
            aria-label="Where to serve this model"
            style={{ marginBottom: 10 }}
          >
            <button
              type="button"
              aria-pressed={destination === 'cluster'}
              onClick={() => setDestination('cluster')}
            >
              Run it here
            </button>
            {routeTargets.map((t) => (
              <button
                key={t.provider_id}
                type="button"
                aria-pressed={destination === t.provider_id}
                onClick={() => setDestination(t.provider_id)}
              >
                Route to {t.display_name}
                {t.served ? ' ✓' : ''}
              </button>
            ))}
          </div>
        ) : null}

        {/* Routing. Deliberately the whole pane: there is no plan to run, no
            machine to pick and no quantization to choose for a model on
            somebody else's hardware, and drawing those anyway would fire a
            debounced plan call and a hub read per repository for a question
            nobody asked. */}
        {routing ? (
          <>
            <RouteCard
              facts={routing}
              catalogue={catalogues?.[routing.provider_id] ?? null}
              catalogueError={
                catalogueErrors?.find((f) => f.provider_id === routing.provider_id)
                  ?.message ?? null
              }
            />
            {/* Self-hiding: a provider kind that does not aggregate backend
                hosts per model answers 400, and this renders nothing rather
                than a control that would fail on every click. */}
            <BackendPicker providerId={routing.provider_id} upstreamId={routing.upstream_id} />
          </>
        ) : (
        <>
        {servedBy.length ? <ProviderFacts rows={servedBy} /> : null}

        {/* No local answer, and there never will be one: the id is not a
            repository anything here can resolve. The resolver's own sentence
            says why, verbatim -- it names the cause and, on a gated repo,
            says to set HF_TOKEN. */}
        {detailError && servedBy.length ? (
          <>
            <div className="sub">no local verdict</div>
            <Verbatim text={detailError} size="unit" />
          </>
        ) : null}

        {detailError && servedBy.length ? null : (
        <ServePanel
          modelId={modelId}
          context={context}
          concurrency={concurrency}
          target={target}
          onTarget={setTarget}
          runtime={runtime}
          onRuntime={(next) => {
            setRuntime(next)
            // The note explains a default this screen chose. Once somebody has
            // chosen for themselves it is describing a decision that is no
            // longer in force, so it goes.
            setRuntimeBecause(null)
          }}
          runtimeBecause={runtimeBecause}
          pullTargets={pullTargets}
          providerId={chosenProvider}
          onProviderId={setProviderId}
          cache={cache}
          cluster={cluster.data}
          initialCustomCommand={initialCustomCommand}
          ladder={ladder}
          loadingLadder={loadingLadder}
          ladderError={ladderError}
        />
        )}
        </>
        )}
      </div>

      <div hidden={pane !== 'about'}>
        {detail ? (
          <>
            <CapabilityChips detail={detail} />

            <div className="sub">what this repository is</div>
            <ParamBreakdownTable detail={detail} />

            <div className="sub">provenance</div>
            <div className="row">
              <span>Parameter count</span>
              <span className="unit">{detail.param_source ?? '—'}</span>
            </div>
            <div className="row">
              <span>Quantization</span>
              <span className="unit">{detail.quant_source ?? '—'}</span>
            </div>
            <div className="row">
              <span>Weights on disk</span>
              <span className="mono">
                {detail.weight_bytes_effective
                  ? `${gbytes(detail.weight_bytes_effective)} GiB`
                  : '—'}
              </span>
            </div>

            <div className="sub">runtimes</div>
            {(detail.support?.runtimes ?? []).map((r) => (
              <div
                key={r.runtime}
                style={{ display: 'flex', gap: 8, alignItems: 'baseline', padding: '3px 0' }}
              >
                <Lamp
                  signal={
                    r.level === 'supported'
                      ? 'live'
                      : r.level === 'unsupported'
                        ? 'fault'
                        : 'idle'
                  }
                  label={`${r.runtime}: ${r.level}`}
                />
                <span className="label" style={{ width: 58 }}>
                  {r.runtime}
                </span>
                {r.version ? (
                  <span className="mono unit" title="the image version this was checked against">
                    {r.version}
                  </span>
                ) : null}
                {/* Verbatim: the runtime's own sentence is more precise than a
                    rewrite, and it is what a refused launch would have said. */}
                <Verbatim text={r.reason} size="label" />
              </div>
            ))}

            {detail.nodes.problems.length || detail.nodes.skipped?.length ? (
              <>
                <div className="sub">on this hardware</div>
                <VerbatimList items={detail.nodes.problems} />
                {skippedLines(detail.nodes.skipped).map((line) => (
                  // Muted, and deliberately not a VerbatimList row: these
                  // machines were not asked, which is not the same as objecting.
                  <div key={line} className="unit" style={{ marginTop: 6 }}>
                    {line}
                  </div>
                ))}
              </>
            ) : null}

            {detail.warnings.length ? (
              <>
                <div className="sub">what had to be assumed</div>
                <VerbatimList items={detail.warnings} />
              </>
            ) : null}
          </>
        ) : detailError ? (
          <>
            <div className="sub">what this repository is</div>
            {/* The resolve failing does not stop the Serve tab from being
                useful, so this is stated where the detail would have been
                rather than in place of the whole pane. */}
            <p
              className="label"
              style={{ fontWeight: 400, color: 'var(--fault)', whiteSpace: 'pre-wrap' }}
            >
              {detailError}
            </p>
          </>
        ) : null}
      </div>
    </>
  )
}


/** Who else serves this model, and on what terms.
 *
 *  Every figure is read off `/api/providers` and none is computed. A null
 *  price prints as "not priced" and never as $0: the wire distinguishes
 *  "never published a price" from "free", and collapsing them would invent a
 *  commercial fact. Nothing key-shaped is rendered -- not `api_key_ref`, not
 *  anything derived from it. */
function ProviderFacts({ rows }: { rows: RowProvider[] }) {
  const price = (v: number | null) => (v == null ? null : `$${v.toFixed(2)}`)
  return (
    <>
      <div className="sub">served elsewhere</div>
      {rows.map((p) => {
        const inp = price(p.input_cost_per_mtok)
        const out = price(p.output_cost_per_mtok)
        return (
          <div key={`${p.provider_id}::${p.served_name}`} style={{ padding: '4px 0' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <Lamp
                signal={p.healthy ? 'live' : 'fault'}
                label={p.healthy ? 'healthy' : 'unhealthy'}
              />
              <span style={{ flex: 1 }}>{p.display_name}</span>
              {/* The string you would actually send as `model` at /v1. The one
                  operationally useful fact on the panel. */}
              <span className="mono unit">{p.served_name}</span>
            </div>
            <div className="unit" style={{ paddingLeft: 20 }}>
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
              <div style={{ paddingLeft: 20 }}>
                <Verbatim text={p.last_error} size="unit" />
              </div>
            ) : null}
            {p.admitting === false && p.admission_block ? (
              <div style={{ paddingLeft: 20 }}>
                <Verbatim text={p.admission_block} size="unit" />
              </div>
            ) : null}
          </div>
        )
      })}
    </>
  )
}
