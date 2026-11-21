import { useEffect, useMemo, useRef, useState } from 'react'
import type {
  Cluster,
  CommandPreview,
  DeploymentDTO,
  EngineOptions,
  LaunchRequest,
  Lifecycle,
  ModelDetail,
  ParallelismRequest,
  PlanRequest,
  PlanResponse,
  Profile,
  Provider,
  ProviderCatalogueModel,
  ProviderKindSpec,
  PullAccepted,
  QuantVariant,
  ServeRequirement,
  VariantLadder,
} from '../../api/types'
import { ApiError } from '../../api/client'
import type { Runtime } from '../../state/runtime'
import { runtimeFor, servesOnCluster } from '../../state/runtime'
import { useBackend } from '../../state/backend'
import { usePlacement } from '../../state/placement'
import { useRouter } from '../../state/router'
import { useCluster, useMemoryReport, useProfiles, useStorage, useTopology } from '../../state/resources'
import { recordCustomServe } from '../../state/customServes'
import { DEFAULT_CONCURRENCY, DEFAULT_CONTEXT } from '../../state/routes'
import { buildBoard, type Board } from './board'
import { canServeWith, gatesOf, refusalCode } from './gates'
import { launchId, ollamaRef, variantKey } from './ollamaTarget'
import { pullableProviders } from './pullTargets'
import { defaultVariant } from './quantPicks'
import type { RouteTargetFacts } from './RouteCard'
import {
  buildLaunchRequest,
  buildPlanRequest,
  knobsFromRequest,
  type FlagRow,
  type LaunchInputs,
} from './launchRequest'
import { changedKeys, preloadChoice } from './profiles'
import {
  cacheIndex,
  providerRowsFor,
  TERMINAL,
  type CacheIndex,
  type RowProvider,
} from './rows'

/** One extra launch flag, as typed under Advanced › Custom flags. */
export type { FlagRow } from './launchRequest'

/** Everything the Serve and Advanced tabs share, owned in one place.
 *
 *  The plan request and the launch request have to agree about every field
 *  that shapes them -- the runtime, the target, the variant, the KV width, the
 *  forced weight scheme, the machines and the degrees -- and two copies of any
 *  of them would let the verdict describe one launch while Serve started
 *  another. So this hook is the only owner, and the components below it only
 *  read and set. */
export interface ModelSession {
  // ── What is being served ──────────────────────────────────────────────────
  detail: ModelDetail | null
  detailError: string | null
  ladder: VariantLadder | null
  ladderError: string | null
  loadingLadder: boolean
  /** The ladder row the verdict is taken for: the one picked, else
   *  `defaultVariant`. Null until the ladder lands, or when it has no rows. */
  variant: QuantVariant | null
  selectVariant: (key: string) => void
  /** The id the plan and the launch are sent: the variant's own (`launchId`),
   *  or the model id while there is no variant. */
  subjectId: string

  // ── How ───────────────────────────────────────────────────────────────────
  runtime: Runtime
  setRuntime: (r: Runtime) => void
  runtimeBecause: string | null
  target: 'throughput' | 'latency'
  setTarget: (t: 'throughput' | 'latency') => void
  pullTargets: Provider[]
  /** Resolved: the picked provider, else the first configured one, else ''. */
  providerId: string
  setProviderId: (id: string) => void
  provider: Provider | null

  // ── Or routed ─────────────────────────────────────────────────────────────
  servedBy: RowProvider[]
  routeTargets: RouteTargetFacts[]
  destination: string
  setDestination: (d: string) => void
  /** The route target `destination` names, or null for the launcher. */
  routing: RouteTargetFacts | null

  // ── The verdict ───────────────────────────────────────────────────────────
  result: PlanResponse | null
  /** The last `kv_dtype_ratios` a plan answered for this model id. Kept across
   *  a plan error -- a refused width nulls `result`, and blanking every packed
   *  row would read as "refused" -- and dropped only when the model changes.
   *  Null until a plan for this id has answered. */
  kvRatios: Record<string, number> | null
  checking: boolean
  error: string | null
  gates: ServeRequirement[]
  granted: Record<string, boolean>
  grant: (param: string, value: boolean) => void
  canServe: boolean

  // ── Launch knobs ──────────────────────────────────────────────────────────
  kvDtype: string
  setKvDtype: (v: string) => void
  forcedQuant: string
  setForcedQuant: (v: string) => void
  /** Whether a forced weight scheme means anything for this variant: only
   *  the base repository's own safetensors can be told to load as another. */
  forcedQuantApplies: boolean
  enforceEager: boolean
  setEnforceEager: (v: boolean) => void
  cudagraphSizesText: string
  setCudagraphSizesText: (v: string) => void
  customCommand: boolean
  setCustomCommand: (v: boolean) => void
  customCommandText: string
  setCustomCommandText: (v: string) => void
  trustRemoteCode: boolean
  setTrustRemoteCode: (v: boolean) => void
  flags: FlagRow[]
  setFlags: (next: FlagRow[]) => void
  servedName: string
  setServedName: (v: string) => void
  /** Typed engine knobs WITHOUT `env`; `envRows` holds that. */
  engineOptions: EngineOptions
  setEngineOptions: (next: EngineOptions) => void
  /** Sent with the launch when set. A running deployment's lifecycle is
   *  PATCHed by the Advanced tab instead. */
  lifecycle: Partial<Lifecycle> | null
  setLifecycle: (next: Partial<Lifecycle> | null) => void
  envRows: FlagRow[]
  setEnvRows: (next: FlagRow[]) => void

  // ── Profiles ──────────────────────────────────────────────────────────────
  profiles: Profile[]
  /** The saved profile the knobs were loaded from, or null. */
  activeProfile: Profile | null
  /** The request the knobs were last loaded from (a profile, a run, a
   *  preload), or null for planner defaults. */
  profileBase: LaunchRequest | null
  /** How `profileBase` arrived. */
  profileSource: 'loaded' | 'default' | 'last-run' | null
  /** `changedKeys(profileBase, currentRequest())`. */
  changed: string[]
  modified: boolean
  /** The launch body the current knobs would send. */
  currentRequest: () => LaunchRequest
  /** Every knob and URL field from a saved request. */
  applyRequest: (req: LaunchRequest, source: Profile | null) => void
  /** Record that the current knobs ARE `profile` now (after a save). */
  markSaved: (profile: Profile) => void
  /** Every knob back to the planner's defaults, URL fields cleared. */
  resetToDefaults: () => void

  // ── Command preview ───────────────────────────────────────────────────────
  commandPreview: CommandPreview | null
  commandPreviewError: string | null
  /** Set by the Advanced tab while it is mounted; the preview is only
   *  fetched while somebody can see it. */
  setWantPreview: (v: boolean) => void
  /** The effective window (YaRN-stretched when set) and the trained one. */
  nativeWindow: number | null
  originalWindow: number | null


  // ── Where ─────────────────────────────────────────────────────────────────
  board: Board
  nodeIds: string[] | null
  degrees: ParallelismRequest | null
  setNodeIds: (next: string[] | null) => void
  setDegrees: (next: ParallelismRequest | null) => void
  cache: CacheIndex
  /** The body `/api/plan` is sent, for companion routes (candidates). */
  planBody: PlanRequest
  cluster: Cluster | null
  liveContext: number
  liveConcurrency: number

  // ── What happened ─────────────────────────────────────────────────────────
  /** This model's deployments, any variant, newest first (launching ones,
   *  which have no `started_at` yet, before all of them). */
  deployments: DeploymentDTO[]
  running: DeploymentDTO | null
  lastFailure: DeploymentDTO | null
  launching: boolean
  pulled: PullAccepted | null
  serve: () => Promise<void>
  stop: (deploymentId: string) => Promise<void>
}

export function useModelSession(p: {
  modelId: string
  /** Already debounced by the list, so this and the quantization ladder are
   *  always answering the same question at the same moment. Null is the
   *  default path and means the coordinator chooses -- so it is spread out of
   *  the plan body rather than sent, exactly like `node_ids`. */
  context: number | null
  concurrency: number | null
  /** Polled by the screen that owns this pane -- `useResource` does not
   *  deduplicate, so mounting `useProviders()` here would fan a second poll
   *  for a list already in memory. */
  providers?: Provider[] | null
  providerKinds?: ProviderKindSpec[] | null
  /** Each provider's WHOLE catalogue, keyed by provider id. It says which
   *  providers publish this model without serving it. */
  catalogues?: Record<string, ProviderCatalogueModel[]> | null
  /** Open on the routing side rather than the launcher. Read once, at mount
   *  -- the pane is keyed on the model id. */
  preferRoute: boolean
  /** Seeds the custom-command field from a custom serve picked on the list
   *  (state/customServes.ts). Read once, at mount. */
  initialCustomCommand?: string
  /** A quantization named on the way in -- a pasted `hf.co/<repo>:<QUANT>`
   *  link -- preferred over the gateway's recommendation when the ladder
   *  lands. */
  preferQuant?: string | null
  /** A saved request to open with (the Profiles list's Load). Read once, at
   *  mount; applied when the ladder lands, in place of the preload. */
  initialRequest?: { request: LaunchRequest; profile: Profile | null }
}): ModelSession {
  const { modelId, context, concurrency, providers, providerKinds, catalogues } = p
  const { backend, invalidate } = useBackend()
  const { route, navigate } = useRouter()
  const { nodeIds, degrees, setNodeIds, setDegrees } = usePlacement()
  // The machines ticked on the board, which are part of the question the
  // ladder answers. Joined into a string for the effect's dependency list: the
  // array identity changes on every render even when the ids have not, and a
  // fresh 20s hub enumeration per render is not a refetch, it is a loop.
  const nodeKey = (nodeIds ?? []).join(',')

  const [detail, setDetail] = useState<ModelDetail | null>(null)
  const [detailError, setDetailError] = useState<string | null>(null)
  const [ladder, setLadder] = useState<VariantLadder | null>(null)
  const [ladderError, setLadderError] = useState<string | null>(null)
  const [loadingLadder, setLoadingLadder] = useState(true)

  // Owned here because the plan call and the launch have to agree about them:
  // `target` changes what the planner ranks, and two copies of it would let
  // the verdict describe one shape while the Serve button starts another.
  const [target, setTarget] = useState<'throughput' | 'latency'>('throughput')
  const [runtime, setRuntimeState] = useState<Runtime>('vllm')
  // Why the runtime above is what it is, when the answer was not the default.
  // Rendered next to the picker rather than inferred from it: a control that
  // moved on its own has to say what moved it, or it reads as a bug.
  const [runtimeBecause, setRuntimeBecause] = useState<string | null>(null)
  // The provider a pull would land on. Empty means "the first configured
  // target", resolved at the point of use rather than seeded, so a provider
  // appearing after this mounted is still offered.
  const [providerPick, setProviderId] = useState('')

  const pullTargets = useMemo(
    () => pullableProviders(providers ?? undefined, providerKinds ?? undefined),
    [providers, providerKinds],
  )
  // A stale id -- the provider was removed -- falls back the same way instead
  // of addressing a machine that is no longer there.
  const provider =
    pullTargets.find((t) => t.provider_id === providerPick) ?? pullTargets[0] ?? null
  const providerId = provider?.provider_id ?? ''

  // Polled once here and handed to every tab. `useResource` does not
  // deduplicate -- every call is its own interval and its own request -- so a
  // second `useStorage` would fan a second cluster-wide disk walk out every
  // 30s for one answer.
  const storage = useStorage(true)
  const clusterRes = useCluster()
  const cluster = clusterRes.data
  const cache = useMemo(() => cacheIndex(storage.data), [storage.data])
  const topology = useTopology()
  const memory = useMemoryReport()

  /** Every provider that publishes this exact id. Joined here rather than
   *  received, so the pane is self-sufficient from a cold URL. */
  const servedBy = useMemo(() => providerRowsFor(providers, modelId), [providers, modelId])

  /** Every provider that could carry this model at `/v1`, served or not.
   *
   *  Assembled from both provider surfaces because neither has all of it. The
   *  filtered listing knows health and admission for what is already served;
   *  the catalogue knows what is merely published. A provider in both is
   *  counted once, on the served side, which is the stronger fact. */
  const routeTargets = useMemo<RouteTargetFacts[]>(() => {
    const chosen = new Map((providers ?? []).map((pr) => [pr.provider_id, pr.models_chosen ?? null]))
    const out: RouteTargetFacts[] = servedBy.map((pr) => ({
      provider_id: pr.provider_id,
      display_name: pr.display_name,
      served_name: pr.served_name,
      // `providerRows` keys its rows on the upstream id, so for a served
      // model that is this pane's own id.
      upstream_id: modelId,
      context_length: pr.context_length,
      input_cost_per_mtok: pr.input_cost_per_mtok,
      output_cost_per_mtok: pr.output_cost_per_mtok,
      supports_tools: pr.supports_tools,
      supports_streaming: pr.supports_streaming,
      served: true,
      healthy: pr.healthy,
      admission_block: pr.admission_block,
      models_chosen: chosen.get(pr.provider_id) ?? null,
    }))
    const seen = new Set(out.map((t) => t.provider_id))
    for (const [pid, rows] of Object.entries(catalogues ?? {})) {
      if (seen.has(pid)) continue
      const m = (rows ?? []).find((r) => r.upstream_id === modelId && !r.enabled)
      if (!m) continue
      out.push({
        provider_id: pid,
        display_name: (providers ?? []).find((pr) => pr.provider_id === pid)?.display_name || pid,
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
        models_chosen: chosen.get(pid) ?? null,
      })
    }
    return out
  }, [servedBy, catalogues, providers, modelId])

  /** Where serving this model would happen: `'cluster'` is the launcher, a
   *  provider id is the allowlist. Seeded once; switching by hand is never
   *  undone by a poll landing. */
  const [destination, setDestination] = useState<string>(
    () => (p.preferRoute ? routeTargets[0]?.provider_id : null) ?? 'cluster',
  )
  // A provider that has gone away must not leave the pane addressing it.
  // Falls back to the launcher rather than to another provider: which
  // provider was a choice, and this screen does not get to make it.
  const routing = routeTargets.find((t) => t.provider_id === destination) ?? null

  // ── Detail and ladder ─────────────────────────────────────────────────────
  //
  // Both are hub-bound and neither belongs in `resources.ts` -- `useResource`
  // refires on every `revision` bump, so an open model would re-run its hub
  // calls after every launch. They are separate and shown separately: detail
  // is one cached resolve, the ladder is a search plus a repository read per
  // GGUF repo and can take seconds.
  const fetchSeq = useRef(0)
  useEffect(() => {
    const mine = ++fetchSeq.current
    setDetail(null)
    setDetailError(null)
    setLadder(null)
    setLadderError(null)
    setLoadingLadder(true)

    const enumerate = () => {
      backend
        // The ticked machines go with the question, so every row is sized
        // where the Serve button would launch it.
        .modelVariants(modelId, { context, concurrency, nodeIds })
        .then((l) => {
          if (fetchSeq.current === mine) setLadder(l)
        })
        .catch((e: unknown) => {
          if (fetchSeq.current === mine) setLadderError(e instanceof Error ? e.message : String(e))
        })
        .finally(() => {
          if (fetchSeq.current === mine) setLoadingLadder(false)
        })
    }

    // `modelDetail` is the authority on whether this id resolves at all.
    const resolving = backend
      .modelDetail(modelId)
      .then((d) => {
        if (fetchSeq.current === mine) {
          setDetail(d)
          // The runtime follows the model, once, when the model changes. A
          // speech checkpoint cannot load on vllm at all, and on a box with no
          // GPU every CUDA runtime is refused on every machine -- so the
          // default is picked on arrival, with its reason, and never re-picked
          // by a poll: choosing vllm to read its refusal is legitimate.
          const pick = runtimeFor(d.modality, cluster?.nodes)
          setRuntimeState(pick.runtime)
          setRuntimeBecause(pick.because)
        }
        return true
      })
      .catch((e: unknown) => {
        if (fetchSeq.current === mine) setDetailError(e instanceof Error ? e.message : String(e))
        return false
      })

    if (!servedBy.length) {
      enumerate()
    } else {
      // A model somebody's provider serves may not exist on the hub at all --
      // `openai/gpt-4o` does not. Firing the ladder for it is a guaranteed
      // second failure, so wait for the one call that can tell us.
      resolving.then((ok) => {
        if (fetchSeq.current !== mine) return
        if (ok) enumerate()
        else setLoadingLadder(false)
      })
    }
    // `cluster` is read once, inside the resolve, on purpose: the roster
    // refreshes on a timer and re-running this would overwrite a runtime
    // somebody had picked.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [backend, modelId, context, concurrency, nodeKey, servedBy.length])

  // ── The variant ───────────────────────────────────────────────────────────
  //
  // Keyed on `variantKey`, never on repo_id -- one GGUF repository publishes
  // many quantizations. Kept across a refetch of the ladder (a new context, a
  // new machine) because the key still names the same file.
  const [variantKeyChosen, setVariantKeyChosen] = useState<string | null>(null)
  const variant = useMemo(
    () =>
      ladder?.variants.find((v) => variantKey(v) === variantKeyChosen) ??
      defaultVariant(ladder, runtime, p.preferQuant ?? null),
    [ladder, variantKeyChosen, runtime, p.preferQuant],
  )
  const subjectId = variant ? launchId(variant) : modelId

  // ── Launch knobs ──────────────────────────────────────────────────────────
  const [result, setResult] = useState<PlanResponse | null>(null)
  const [keptRatios, setKeptRatios] = useState<{ id: string; ratios: Record<string, number> } | null>(null)
  const kvRatios = keptRatios?.id === subjectId ? keptRatios.ratios : null
  const [checking, setChecking] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [launching, setLaunching] = useState(false)
  // What the pull endpoint accepted, when the runtime pulls. A pulled model
  // never becomes a Deployment, so this is the only record there will be.
  const [pulled, setPulled] = useState<PullAccepted | null>(null)
  // Every override granted against this measurement, keyed by the launch field
  // it unlocks. Cleared wholesale on every replan below, so none can outlive
  // the number that justified it.
  const [granted, setGranted] = useState<Record<string, boolean>>({})
  // Permissions a launch or a pull was REFUSED for after the plan said yes:
  // a 409 whose code names an override. Cleared with `granted`.
  const [extraGates, setExtraGates] = useState<ServeRequirement[]>([])
  // Raw CLI text that replaces the generated command, for a model the
  // standard recipe doesn't cover. Local state, not URL-linked: it has no
  // sizing implication to echo back on reload.
  const [customCommandText, setCustomCommandText] = useState(() => p.initialCustomCommand ?? '')
  // Whether the custom command is in use. Separate from the text so
  // unticking it disables the tokens without discarding what was typed.
  // Starts ticked when a custom serve seeded a value.
  const [customCommand, setCustomCommand] = useState(() => !!p.initialCustomCommand)
  // Whether this launch skips CUDA graph capture entirely, and the raw text
  // of a trimmed capture-size list when it does not. Launch-only: neither
  // changes anything the gate priced.
  const [enforceEager, setEnforceEager] = useState(false)
  const [cudagraphSizesText, setCudagraphSizesText] = useState('')
  // The KV cache element width. NOT the same tier as the two above: this one
  // is a fit-gate input -- it halves bytes-per-token, so it changes the
  // verdict, the context the coordinator picks, and the byte budget the launch
  // is handed. It therefore goes into BOTH requests below, with the same
  // value, or the gate and the engine disagree about how wide a cache entry
  // is and the deployment silently serves half the approved context.
  // Empty string means "the coordinator's configured default".
  const [kvDtype, setKvDtype] = useState('')
  // The weight quantization to force, or '' for the checkpoint's own packing.
  // Exactly the tier `kvDtype` is and for a bigger term: every weight figure
  // in the verdict is computed at this scheme, so it goes into BOTH requests.
  // Until 2026-09-11 the override reached the plan and not the launch, so a
  // plan approved at nvfp4 started a bf16 checkpoint against a budget sized
  // for something 3.5x smaller.
  const [forcedQuant, setForcedQuant] = useState('')
  // A forced scheme is a resolver override of the BASE checkpoint's packing.
  // A quantization repository, or a GGUF file, already is a scheme: forcing
  // another onto it would price weights nobody published.
  const forcedQuantApplies =
    variant == null || (variant.repo_id === modelId && !variant.gguf_file)
  useEffect(() => {
    if (!forcedQuantApplies && forcedQuant) setForcedQuant('')
  }, [forcedQuantApplies, forcedQuant])
  const sentQuant = forcedQuantApplies ? forcedQuant : ''
  // Extra flags appended to the generated command. `extra_args` on the wire,
  // checked token by token by the coordinator.
  const [flags, setFlags] = useState<FlagRow[]>([])
  // The name clients pass as `model`. Blank lets the coordinator pick one.
  const [servedName, setServedName] = useState('')
  const [engineOptions, setEngineOptions] = useState<EngineOptions>({})
  const [lifecycle, setLifecycle] = useState<Partial<Lifecycle> | null>(null)
  const [envRows, setEnvRows] = useState<FlagRow[]>([])
  const [activeProfile, setActiveProfile] = useState<Profile | null>(null)
  const [profileBase, setProfileBase] = useState<LaunchRequest | null>(null)
  const [profileSource, setProfileSource] = useState<'loaded' | 'default' | 'last-run' | null>(null)
  const [wantPreview, setWantPreview] = useState(false)
  const [commandPreview, setCommandPreview] = useState<CommandPreview | null>(null)
  const [commandPreviewError, setCommandPreviewError] = useState<string | null>(null)
  const profilesRes = useProfiles(modelId)
  const profiles = useMemo(() => profilesRes.data ?? [], [profilesRes.data])
  // The loaded profile as the server holds it NOW: a rename shows its new
  // name, and a deleted one stops being "Profile: <name>" in the footer. Kept
  // as loaded while the list has not answered yet, so it does not flicker.
  const liveProfile = useMemo(
    () =>
      activeProfile && profilesRes.data
        ? (profilesRes.data.find((p) => p.profile_id === activeProfile.profile_id) ?? null)
        : activeProfile,
    [activeProfile, profilesRes.data],
  )
  // A custom command replaces every plan-derived flag, so a verdict sized at a
  // width that command cannot carry is a verdict for a different launch. It
  // is in the plan effect's dependency list for that reason.
  const usingCustomCommand = customCommand && customCommandText.trim().length > 0
  // Opt-in to the repo's own Python. Off by default, except under `tts`:
  // every architecture that server supports is remote code transformers does
  // not ship. Reset whenever the runtime changes so a tick under one runtime
  // never rides into another.
  const [trustRemoteCode, setTrustRemoteCode] = useState(() => runtime === 'tts')
  // `applyRequest` sets the runtime and the tick together; the reset below
  // must not undo the tick it just restored.
  const keepTrust = useRef(false)
  useEffect(() => {
    if (keepTrust.current) {
      keepTrust.current = false
      return
    }
    setTrustRemoteCode(runtime === 'tts')
  }, [runtime])

  // What the context fields show. `route.context` is the override, or null
  // for "the coordinator chose"; then the boxes show what it actually chose,
  // taken off the plan result, and fall back to the placeholder only until
  // the first answer lands.
  const liveContext = route.context ?? result?.context ?? DEFAULT_CONTEXT
  const liveConcurrency = route.concurrency ?? result?.concurrency ?? DEFAULT_CONCURRENCY

  // The speculative selection as the wire wants it. Memoized on the
  // primitives rather than carried as `route.spec`, which `parse()` rebuilds
  // on every navigation: an object identity in the plan effect's dependency
  // list would refire a plan request on every unrelated URL change, forever.
  const specMethod = route.spec?.method ?? null
  const specTokens = route.spec?.tokens ?? null
  // Empty string is the "external picked, nothing typed yet" state. It must
  // not become a request: the coordinator would resolve `""` and refuse it.
  const specHead = route.spec?.model || null
  const specPending = route.spec?.model === ''
  const speculative = useMemo(
    () =>
      specMethod && specTokens && !specPending
        ? {
            method: specMethod,
            num_speculative_tokens: specTokens,
            ...(specHead ? { model: specHead } : {}),
          }
        : null,
    [specMethod, specTokens, specHead, specPending],
  )

  // ── The plan ──────────────────────────────────────────────────────────────
  // Built by the same function launch is, so the card judges the runtime and
  // sizing the button will send. Context and concurrency stay absent when
  // unset ("the planner picks"); a custom command suppresses kv_dtype, dtype,
  // speculation and engine options exactly as it does on launch. Keyed on
  // its JSON so a fresh-but-equal object does not refire the plan.
  const planJson = JSON.stringify(
    buildPlanRequest({
      modelId: subjectId,
      target,
      runtime,
      context: context ?? 0,
      concurrency: concurrency ?? 0,
      nodeIds,
      degrees,
      speculative,
      customCommand,
      customCommandText,
      flags: [],
      servedName: '',
      enforceEager: false,
      cudagraphCaptureSizes: null,
      kvDtype,
      forcedQuant: sentQuant,
      speculativeTokens: 0,
      trustRemoteCode: false,
      engineOptions,
      envRows,
      lifecycle: null,
    }),
  )
  const planBody = useMemo<PlanRequest>(() => JSON.parse(planJson) as PlanRequest, [planJson])

  const noLocalVerdict = detailError != null && servedBy.length > 0
  const planSeq = useRef(0)
  useEffect(() => {
    const mine = ++planSeq.current
    // Nothing to plan under a provider runtime: no machine here carries it,
    // and a verdict from this gate would be about the wrong memory pool while
    // looking exactly like one that meant something.
    // Nor for an id that does not resolve while a provider serves it
    // (`openai/gpt-4o`): there is no local verdict to have, and the pane says
    // so in the resolver's own words instead of asking the planner to fail.
    if (!servesOnCluster(runtime) || noLocalVerdict) {
      setChecking(false)
      setResult(null)
      setError(null)
      setGranted({})
      setExtraGates([])
      return
    }
    setChecking(true)
    setGranted({})
    setExtraGates([])
    const planFor = subjectId
    const id = window.setTimeout(() => {
      backend
        .plan(planBody)
        .then((r) => {
          if (planSeq.current !== mine) return
          setResult(r)
          if (r.kv_dtype_ratios) setKeptRatios({ id: planFor, ratios: r.kv_dtype_ratios })
          setError(null)
        })
        .catch((e: unknown) => {
          if (planSeq.current !== mine) return
          setResult(null)
          setError(e instanceof Error ? e.message : String(e))
        })
        .finally(() => {
          if (planSeq.current === mine) setChecking(false)
        })
    }, 450)
    return () => window.clearTimeout(id)
  }, [backend, planBody, runtime, noLocalVerdict, subjectId])

  // The command derate would run, only while the Advanced tab is showing it.
  // Fetched after the plan settles, so a refusal reads the same as the
  // verdict's and is never a half-typed field's.
  const previewSeq = useRef(0)
  useEffect(() => {
    const mine = ++previewSeq.current
    if (!wantPreview || !result || checking || !servesOnCluster(runtime)) return
    const id = window.setTimeout(() => {
      backend
        .planCommand(planBody)
        .then((r) => {
          if (previewSeq.current !== mine) return
          setCommandPreview(r)
          setCommandPreviewError(null)
        })
        .catch((e: unknown) => {
          if (previewSeq.current !== mine) return
          setCommandPreview(null)
          setCommandPreviewError(e instanceof Error ? e.message : String(e))
        })
    }, 600)
    return () => window.clearTimeout(id)
  }, [backend, planBody, wantPreview, result, checking, runtime])

  // A ticked machine can leave the cluster. Drop it rather than planning
  // against a name nothing answers to. The `every` short-circuit is not an
  // optimisation: without it this writes a fresh array on every 5s cluster
  // poll, which refires the plan effect, forever.
  const nodes = useMemo(() => cluster?.nodes ?? [], [cluster])
  useEffect(() => {
    if (nodeIds == null) return
    const live = new Set(nodes.map((n) => n.profile.node_id))
    if (nodeIds.every((nid) => live.has(nid))) return
    const kept = nodeIds.filter((nid) => live.has(nid))
    setNodeIds(kept.length ? kept : null)
  }, [nodes, nodeIds, setNodeIds])

  const repoId = variant?.repo_id ?? modelId
  const board = useMemo(
    () =>
      buildBoard({
        nodes,
        memory: memory.data?.nodes ?? [],
        edges: topology.data?.edges ?? [],
        deployments: cluster?.deployments ?? [],
        cache,
        repoId,
        plannerChose: result?.plan.node_ids ?? null,
        placement: result?.placement ?? null,
        chosen: nodeIds,
        runtime,
      }),
    [nodes, memory.data, topology.data, cluster, cache, repoId, result, nodeIds, runtime],
  )

  // The permissions this launch needs: the plan's own, then any a refused
  // launch named, each once.
  const serveDecision = result?.serve
  const gates = useMemo(() => {
    const out = [...gatesOf(serveDecision)]
    for (const g of extraGates) if (!out.some((o) => o.param === g.param)) out.push(g)
    return out
  }, [serveDecision, extraGates])

  const onCluster = servesOnCluster(runtime)
  // Under a provider runtime there is no plan: what decides the button is
  // whether the row is a GGUF there is somewhere to send, and whether every
  // refusal the pull came back with has been read and ticked.
  const canServe = onCluster
    ? canServeWith(result?.serve, result?.fit ?? null, gates, granted)
    : Boolean(provider && variant && ollamaRef(variant)) &&
      gates.every((g) => granted[g.param] === true)

  // ── This model's deployments ──────────────────────────────────────────────
  const deployments = useMemo(() => {
    const ids = new Set([modelId, ...(ladder?.variants ?? []).map(launchId)])
    return (cluster?.deployments ?? [])
      .filter((d) => ids.has(d.model_id))
      .sort((a, b) =>
        a.started_at == null
          ? b.started_at == null
            ? 0
            : -1
          : b.started_at == null
            ? 1
            : b.started_at - a.started_at,
      )
  }, [cluster, ladder, modelId])
  const running = deployments.find((d) => !TERMINAL.has(d.state)) ?? null
  const lastFailure = running ? null : (deployments.find((d) => d.state === 'failed') ?? null)

  // A 409 whose code names an override is a permission to ask for, not an
  // error: the request was legal and an unchanged retry succeeds once memory
  // frees or the box is ticked. A static refusal is a 400 and correctly is not.
  const refused = (e: unknown): boolean => {
    const param = e instanceof ApiError && e.status === 409 ? refusalCode(e) : null
    if (!param) return false
    const reason = e instanceof Error ? e.message : String(e)
    setExtraGates((gs) => [...gs.filter((g) => g.param !== param), { param, reason }])
    setGranted((g) => ({ ...g, [param]: false }))
    return true
  }

  const pull = async () => {
    // A different verb. Nothing is planned, nothing is launched, and no
    // Deployment appears -- the provider fetches the weights onto itself and
    // the gateway routes to it once its catalogue refreshes.
    const ref = variant ? ollamaRef(variant) : null
    if (!provider || !ref) {
      setError(
        provider
          ? 'That row has no GGUF file, so there is nothing for a provider to fetch.'
          : 'No provider is configured to pull onto. Add one under Settings → Providers.',
      )
      return
    }
    setLaunching(true)
    setError(null)
    try {
      const reply = await backend.pullToProvider(provider.provider_id, {
        model: ref,
        ...(granted.allow_over_memory === true ? { allow_over_memory: true } : {}),
      })
      setPulled(reply)
      setExtraGates([])
      setGranted({})
      invalidate()
    } catch (e) {
      if (!refused(e)) setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLaunching(false)
    }
  }

  /** Every launch input from the current knobs. Context, concurrency and
   *  speculation are read off `result` when there is one: that is what the
   *  verdict was taken with, and the coordinator may have chosen or corrected
   *  them. */
  const launchInputs = (cudagraphCaptureSizes: number[] | null, grants: string[]): LaunchInputs => ({
    modelId: subjectId,
    context: result?.context ?? liveContext,
    concurrency: result?.concurrency ?? liveConcurrency,
    target,
    runtime,
    nodeIds,
    degrees,
    customCommand,
    customCommandText,
    flags,
    servedName,
    enforceEager,
    cudagraphCaptureSizes,
    kvDtype,
    forcedQuant: sentQuant,
    speculative: result?.speculative
      ? {
          method: result.speculative.method,
          num_speculative_tokens: result.speculative.num_speculative_tokens,
          ...(result.speculative.model ? { model: result.speculative.model } : {}),
        }
      : result
        ? null
        : speculative,
    speculativeTokens: 0,
    // Sent only when the resolver said there is remote code AND the box is
    // ticked; absent means false on the server.
    trustRemoteCode: Boolean((result ? result.remote_code : true) && trustRemoteCode),
    engineOptions,
    lifecycle,
    envRows,
    grants,
  })

  /** The trimmed capture sizes, or null; a malformed list is null here and
   *  an error only at launch. */
  const parsedSizes = (): number[] | null => {
    if (usingCustomCommand || enforceEager || !cudagraphSizesText.trim()) return null
    const parsed = cudagraphSizesText.trim().split(/[\s,]+/).map(Number)
    return parsed.some((n) => !Number.isInteger(n) || n <= 0) ? null : parsed
  }

  const currentRequest = () => buildLaunchRequest(launchInputs(parsedSizes(), []))

  const launch = async () => {
    if (!result) return
    const cudagraphCaptureSizes = parsedSizes()
    if (!usingCustomCommand && !enforceEager && cudagraphSizesText.trim() && !cudagraphCaptureSizes) {
      setError(
        'Trimmed CUDA graph sizes must be positive whole numbers, ' +
          'separated by spaces or commas.',
      )
      return
    }
    setLaunching(true)
    setError(null)
    try {
      const dep = await backend.launch(
        buildLaunchRequest(
          launchInputs(
            cudagraphCaptureSizes,
            // Filtered against the current gates, so a key granted against a
            // previous plan cannot ride along with this one.
            gates.filter((g) => granted[g.param] === true).map((g) => g.param),
          ),
        ),
      )
      invalidate()
      // Worth replaying only once the backend has accepted it, and only if
      // the box was ticked -- exactly the condition that decided whether it
      // was sent above.
      const trimmedCommand = customCommandText.trim()
      if (customCommand && trimmedCommand) {
        recordCustomServe({ modelId, runtime, target, command: trimmedCommand })
      }
      // Take you to the thing you just started. `served_name`, NOT
      // `deployment_id`: `?dep=` is matched against the served name
      // (state/selection.tsx), and an id there falls through to somebody
      // else's deployment.
      if (dep?.served_name) navigate({ dest: 'cluster', dep: dep.served_name })
    } catch (e) {
      if (!refused(e)) setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLaunching(false)
    }
  }

  /** Every knob and URL field from a saved request. The URL half is replaced
   *  rather than pushed: loading a setup is not a place to go back to. */
  const applyRequest = (req: LaunchRequest, source: Profile | null, how: 'loaded' | 'default' | 'last-run' = 'loaded') => {
    const { knobs, route: fields, variant: v } = knobsFromRequest(req, ladder)
    if (v) setVariantKeyChosen(variantKey(v))
    setTarget(knobs.target === 'latency' ? 'latency' : 'throughput')
    if (knobs.runtime && knobs.runtime !== runtime) {
      keepTrust.current = true
      setRuntimeState(knobs.runtime as Runtime)
      setRuntimeBecause(null)
    }
    setCustomCommand(knobs.customCommand)
    setCustomCommandText(knobs.customCommandText)
    setFlags(knobs.flags)
    setServedName(knobs.servedName)
    setEnforceEager(knobs.enforceEager)
    setCudagraphSizesText((knobs.cudagraphCaptureSizes ?? []).join(' '))
    setKvDtype(knobs.kvDtype)
    setForcedQuant(knobs.forcedQuant)
    setTrustRemoteCode(knobs.trustRemoteCode)
    setEngineOptions(knobs.engineOptions)
    setLifecycle(knobs.lifecycle)
    setEnvRows(knobs.envRows)
    navigate(fields, { replace: true })
    setActiveProfile(source)
    setProfileBase(req)
    setProfileSource(how)
  }

  const resetToDefaults = () => {
    setVariantKeyChosen(null)
    setTarget('throughput')
    setCustomCommand(false)
    setCustomCommandText('')
    setFlags([])
    setServedName('')
    setEnforceEager(false)
    setCudagraphSizesText('')
    setKvDtype('')
    setForcedQuant('')
    setEngineOptions({})
    setLifecycle(null)
    setEnvRows([])
    navigate(
      { context: null, concurrency: null, on: null, tp: null, pp: null, ep: null, spec: null },
      { replace: true },
    )
    setActiveProfile(null)
    setProfileBase(null)
    setProfileSource(null)
  }

  // ── Preload ───────────────────────────────────────────────────────────────
  // Once per mount, and only when the URL names none of the setup: a link
  // that does is somebody's explicit question and must not be overwritten.
  const preloaded = useRef(false)
  const initialRequest = useRef(p.initialRequest)
  const urlNamesSetup =
    route.context != null || route.concurrency != null || route.on != null || route.tp != null || route.spec != null
  const ladderSettled = ladder != null || ladderError != null || (!loadingLadder && detail != null)
  useEffect(() => {
    if (preloaded.current || !ladderSettled) return
    const given = initialRequest.current
    if (given) {
      preloaded.current = true
      applyRequest(given.request, given.profile)
      return
    }
    if (urlNamesSetup) {
      preloaded.current = true
      return
    }
    if (profilesRes.data == null && profilesRes.error == null) return
    if (cluster == null) return
    preloaded.current = true
    const choice = preloadChoice(profiles)
    if (choice.request && choice.source) {
      const star = choice.source === 'default' ? (profiles.find((x) => x.is_default) ?? null) : null
      applyRequest(choice.request, star, choice.source)
    }
    // applyRequest is rebuilt every render; the guard above makes this once.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ladderSettled, urlNamesSetup, profilesRes.data, profilesRes.error, cluster, deployments, profiles])

  const changed = profileBase ? changedKeys(profileBase, currentRequest()) : []

  const serve = () => (onCluster ? launch() : pull())

  const stop = async (deploymentId: string) => {
    try {
      await backend.stopDeployment(deploymentId)
      invalidate()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  return {
    detail,
    detailError,
    ladder,
    ladderError,
    loadingLadder,
    variant,
    selectVariant: setVariantKeyChosen,
    subjectId,
    runtime,
    setRuntime: (r) => {
      setRuntimeState(r)
      // The note explains a default this screen chose. Once somebody has
      // chosen for themselves it describes a decision no longer in force.
      setRuntimeBecause(null)
    },
    runtimeBecause,
    target,
    setTarget,
    pullTargets,
    providerId,
    setProviderId,
    provider,
    servedBy,
    routeTargets,
    destination,
    setDestination,
    routing,
    result,
    kvRatios,
    checking,
    error,
    gates,
    granted,
    grant: (param, value) => setGranted((g) => ({ ...g, [param]: value })),
    canServe,
    kvDtype,
    setKvDtype,
    forcedQuant,
    setForcedQuant,
    forcedQuantApplies,
    enforceEager,
    setEnforceEager,
    cudagraphSizesText,
    setCudagraphSizesText,
    customCommand,
    setCustomCommand,
    customCommandText,
    setCustomCommandText,
    trustRemoteCode,
    setTrustRemoteCode,
    flags,
    setFlags,
    servedName,
    setServedName,
    board,
    nodeIds,
    degrees,
    setNodeIds,
    setDegrees,
    planBody,
    cache,
    cluster,
    liveContext,
    liveConcurrency,
    deployments,
    running,
    lastFailure,
    launching,
    pulled,
    serve,
    stop,
    engineOptions,
    setEngineOptions,
    lifecycle,
    setLifecycle,
    envRows,
    setEnvRows,
    profiles,
    activeProfile: liveProfile,
    profileBase,
    profileSource,
    changed,
    modified: changed.length > 0,
    currentRequest,
    applyRequest: (req, source) => applyRequest(req, source),
    markSaved: (profile) => {
      setActiveProfile(profile)
      setProfileBase(profile.request)
      setProfileSource('loaded')
      invalidate()
    },
    resetToDefaults,
    commandPreview,
    commandPreviewError,
    setWantPreview,
    nativeWindow: result?.native_window ?? null,
    originalWindow: result?.original_window ?? null,
  }
}
