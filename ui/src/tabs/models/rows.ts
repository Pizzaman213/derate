import type {
  CapacityReport,
  CapacityRow,
  Cluster,
  CuratedModel,
  DeploymentState,
  ModelRegistryResponse,
  ModelSearchResponse,
  Provider,
  ProviderCatalogueModel,
  ProviderModel,
  RowDeployment,
  RowOffer,
  RowProvider,
  StorageReport,
  Verdict,
} from '../../api/types'

/** Where a model was found.
 *
 *  A merged row carries every facet that applies, because after the merge
 *  "where did this come from" has up to five simultaneous answers and a scalar
 *  cannot hold them: `openai/gpt-oss-120b` on this cluster is curated, cached
 *  on disk AND serving right now. These are provenance, never a verdict -- the
 *  band is the verdict. */
export type Facet =
  | 'running'
  | 'ondisk'
  | 'catalog'
  | 'provider'
  | 'offered'
  | 'hub'

/** The order facets are canonicalised into, and the order the builders are
 *  merged in: local, polled, structured facts before a search payload.
 *
 *  `offered` sits after `provider` and before `hub` because it is a structured
 *  fact off an endpoint, not a search hit -- but it is the weakest of them, and
 *  a model that is both served and merely offered is served. */
export const FACET_ORDER: Facet[] = [
  'running',
  'ondisk',
  'catalog',
  'provider',
  'offered',
  'hub',
]

/** Deployment states that are over. A row whose only deployment is one of
 *  these does not claim the "Running here" band -- it bands by its verdict and
 *  says what went wrong on the row instead. Exported because `board.ts` needs
 *  the same definition of "over": a stopped deployment holds no machine, and
 *  two answers to that question would put the board and this list at odds. */
export const TERMINAL: ReadonlySet<DeploymentState> = new Set<DeploymentState>([
  'failed',
  'stopped',
])

/** The three row shapes now arrive from `GET /api/models` and are declared
 *  with the rest of the wire in `api/types.ts`. Re-exported here so that every
 *  existing `import { type RowProvider } from './rows'` keeps working —
 *  `ModelInspector`, `support.ts` and `rows.check.mjs` among them. */
export type { RowDeployment, RowOffer, RowProvider } from '../../api/types'

/** The band a row draws under inside its source.
 *
 *  `unchecked` is not a fourth verdict, it is the absence of one:
 *  `GET /api/models/search` never resolves anything (an asserted contract, not
 *  an implementation detail), so a hub row genuinely has no fit answer. Drawing
 *  it as a guess would be inventing the one number this UI is not allowed to
 *  invent. */
export type Band =
  | 'running'
  | 'fits'
  | 'degraded'
  | 'unchecked'
  | 'wont'
  | 'elsewhere'
  | 'unserved'

/** One model, with the facts kept apart instead of pre-joined into a string.
 *
 *  The list this feeds used to receive `{label, detail, group}` with `detail`
 *  already concatenated, which is why it could never badge fit, sort by size,
 *  or say what was on disk. Every field below is either read from the wire or
 *  joined from another payload by exact key -- nothing here is computed from a
 *  model's name or its bytes.
 *
 *  One row per MODEL, not per source. The five builders each produce partial
 *  rows and `mergeRows` folds them by model id, so a model that is curated,
 *  cached and serving is one row that says all three rather than three rows
 *  that each say a third of it. */
export interface ModelRow {
  /** The merge key, and React's key. Identical to `model_id` today; kept as
   *  its own field so that if identity ever folds case, the merged row is
   *  keyed by the folded form while `model_id` stays the id to display. */
  key: string
  model_id: string
  label: string
  /** Deduplicated, in FACET_ORDER, never empty. */
  where: Facet[]
  /** Every name this model answers to at `/v1` -- deployment served names and
   *  provider served names. Searched, so typing a served name finds the repo. */
  servedNames: string[]

  // ── Fit. Read from `/api/capacity`, never derived. ────────────────────────
  verdict: Verdict | null
  /** The gate's own sentence. Rendered through `<Verbatim>`. */
  reason: string | null
  /** `'live'` when the verdict was taken against what the node can hand out
   *  right now, `'static'` when only the idle-hardware ceiling was available. */
  basis: 'live' | 'static' | null
  predicted_decode_tps: number | null
  headroom: number | null
  total_params: number | null
  /** The quantization the capacity walk had to step down to, when it stepped.
   *  A SUGGESTION, not a fact about the repository -- never classify the
   *  repository's format from it. */
  dtype: string | null
  /** What the repository actually holds. This is the one to reason about. */
  nativeDtype: string | null
  requantized: boolean
  warnings: string[]
  /** A batched capacity request naming this model is in flight. Not a fourth
   *  verdict and not a band: the row keeps its place and its hollow lamp, and
   *  says "checking" instead of "not checked". */
  checking: boolean

  // ── On disk. Joined from `/api/storage` by repo id. ───────────────────────
  cachedOn: string[]
  bytesOnDisk: number | null

  // ── Running here, and served elsewhere. ───────────────────────────────────
  deployments: RowDeployment[]
  providers: RowProvider[]
  /** Providers that publish this model without serving it. Empty for almost
   *  every row; the whole of a row that exists only because a provider's
   *  catalogue mentions it. */
  offers: RowOffer[]

  // ── Hub facts that were on the type and rendered nowhere. ─────────────────
  downloads: number | null
  likes: number | null
  gated: boolean | string | null
  tags: string[]
  pipelineTag: string | null
  quantHint: string | null

  /** The curated one-liner. Only a catalog contributor sets one. */
  detail: string
  default_context?: number
  default_concurrency?: number

  /** `where` is exactly `['provider']`: nothing here can resolve, cache or
   *  launch it, so there is no local verdict to have and never will be. It
   *  still opens -- the pane shows who serves it and the resolver's own
   *  sentence for why there is no verdict. */
  remoteOnly: boolean

  /** `where` is exactly `['offered']`: the ONLY reason this row exists is that
   *  a provider's catalogue mentions it. Nothing serves it, nothing here has
   *  it on disk, nobody curated it and no search returned it.
   *
   *  This is what keeps a 400-model price sheet out of the list. Such a row
   *  bands last and the band draws collapsed, so the screen gains one line
   *  rather than four hundred -- while the model stays findable by search and
   *  one click from being served. */
  unservedOnly: boolean
}

const EMPTY = {
  verdict: null,
  reason: null,
  basis: null,
  predicted_decode_tps: null,
  headroom: null,
  total_params: null,
  dtype: null,
  nativeDtype: null,
  requantized: false,
  warnings: [] as string[],
  checking: false,
  cachedOn: [] as string[],
  bytesOnDisk: null,
  deployments: [] as RowDeployment[],
  providers: [] as RowProvider[],
  offers: [] as RowOffer[],
  downloads: null,
  likes: null,
  gated: null,
  tags: [] as string[],
  pipelineTag: null,
  quantHint: null,
  detail: '',
  remoteOnly: false,
  unservedOnly: false,
} satisfies Omit<ModelRow, 'key' | 'model_id' | 'label' | 'where' | 'servedNames'>

// ── Joins ────────────────────────────────────────────────────────────────────

export interface CapacityIndex {
  row(modelId: string): CapacityRow | null
  /** The basis of the first report that had one -- what the caption is about. */
  basis: 'live' | 'static' | null
  /** The basis of the report THIS model's verdict came from, so a row never
   *  claims a basis its own answer was not taken at. */
  basisOf(modelId: string): 'live' | 'static' | null
  /** Why a model in the catalogue has no verdict at all, in the walk's words. */
  unresolved(modelId: string): string | null
}

/** Index one or more capacity walks by model id.
 *
 *  Prefers the live side of each report, because "what can I launch right now"
 *  is the question a picker is being asked; falls back to the static ceiling
 *  when the live figure is unavailable, and says which it used rather than
 *  presenting one as the other. A cold coordinator with no live reading still
 *  gets verdicts.
 *
 *  Variadic because the merged list has two sources of verdicts: the 20s
 *  cluster-wide poll, and the targeted batches the auto-check fires for models
 *  nobody curated. Later reports win for a model they both name -- a batch
 *  asked about this model specifically and is fresher than a walk that
 *  happened to include it. */
export function capacityIndex(
  ...reports: (CapacityReport | null | undefined)[]
): CapacityIndex {
  const byId = new Map<string, CapacityRow>()
  const basisById = new Map<string, 'live' | 'static'>()
  const unresolved = new Map<string, string>()
  let basis: 'live' | 'static' | null = null

  for (const report of reports) {
    if (!report) continue
    const side = report.live ?? report.static ?? null
    const reportBasis = report.live ? 'live' : report.static ? 'static' : null
    if (basis === null) basis = reportBasis
    for (const r of side?.rows ?? []) {
      byId.set(r.model_id, r)
      if (reportBasis) basisById.set(r.model_id, reportBasis)
      // A model that resolved in a later report is no longer unresolved.
      unresolved.delete(r.model_id)
    }
    for (const u of report.unresolved ?? []) {
      // ...and one that has a verdict is not overwritten by a stale refusal.
      if (!byId.has(u.model_id)) unresolved.set(u.model_id, u.reason)
    }
  }

  return {
    row: (id) => byId.get(id) ?? null,
    basis,
    basisOf: (id) => basisById.get(id) ?? null,
    unresolved: (id) => unresolved.get(id) ?? null,
  }
}

export interface CacheIndex {
  nodes(repoId: string): string[]
  bytes(repoId: string): number | null
  /** Whether what is on disk is the whole download, given a size that was
   *  actually measured. Only the variant ladder knows one; a base repository
   *  has no expected size to check against, and `null` says so rather than
   *  guessing. */
  complete(repoId: string, expectedBytes: number | null | undefined): boolean | null
  /** Every cached repository, largest first, for the On device source. */
  all(): { repo_id: string; bytes: number; nodes: string[] }[]
}

/** How much of the expected bytes must be present before a cache entry counts
 *  as the download rather than part of one. Not 100%: a cache holds symlinked
 *  snapshots whose reported sizes jitter, and an exact test would call a
 *  finished download partial. */
const COMPLETE_FRACTION = 0.99

/** Index the downloaded weights by repository id.
 *
 *  `/api/storage` already fans out to every node agent and reports `repo_id`
 *  per cached folder; this is a join on that, not a second measurement.
 *
 *  Bytes are the largest figure any node reports, not the sum. Two node records
 *  can share one physical cache -- this cluster registers `spark-4d38` and
 *  `probe-worker` on the same host, both reporting the same 52 repositories --
 *  and summing there would claim 364 GiB for a 182 GiB download. The question
 *  this figure answers is "how big is this model", which is a per-repository
 *  fact; how many nodes hold it is `nodes()`, carried separately. */
export function cacheIndex(report: StorageReport | null | undefined): CacheIndex {
  const nodes = new Map<string, string[]>()
  const bytes = new Map<string, number>()
  for (const n of report?.nodes ?? []) {
    for (const repo of n.models?.repos ?? []) {
      if (!repo.repo_id) continue
      // `blob_count: 0` is a cache directory with no files in it -- a resolve
      // touched the repo and wrote nothing. Four of this cluster's 52 are in
      // that state, and counting them as cached made the card say "already on
      // spark-4d38, the first launch does not have to pull it" about a model
      // of which not one byte is present. Exact, not a size threshold.
      if (repo.blob_count === 0) continue
      const list = nodes.get(repo.repo_id)
      if (list) list.push(n.node_id)
      else nodes.set(repo.repo_id, [n.node_id])
      bytes.set(repo.repo_id, Math.max(bytes.get(repo.repo_id) ?? 0, repo.bytes))
    }
  }
  return {
    nodes: (id) => nodes.get(id) ?? [],
    bytes: (id) => bytes.get(id) ?? null,
    complete: (id, expected) => {
      if (expected == null || expected <= 0) return null
      const have = bytes.get(id)
      if (have == null) return false
      return have >= expected * COMPLETE_FRACTION
    },
    all: () =>
      [...nodes.entries()]
        .map(([repo_id, ns]) => ({ repo_id, bytes: bytes.get(repo_id) ?? 0, nodes: ns }))
        .sort((a, b) => b.bytes - a.bytes),
  }
}

function withFit(row: ModelRow, cap: CapacityIndex, cache: CacheIndex): ModelRow {
  const c = cap.row(row.model_id)
  const cachedOn = cache.nodes(row.model_id)
  return {
    ...row,
    cachedOn,
    bytesOnDisk: cache.bytes(row.model_id),
    ...(c
      ? {
          // `'error'` is a walk failure, not a verdict; it must not draw as one.
          verdict: c.verdict === 'error' ? null : c.verdict,
          reason: c.reason,
          // This model's OWN report, not whichever report came first.
          basis: cap.basisOf(row.model_id) ?? cap.basis,
          predicted_decode_tps: c.predicted_decode_tps,
          headroom: c.headroom,
          total_params: c.total_params,
          dtype: c.dtype,
          nativeDtype: c.native_dtype,
          requantized: c.requantized,
          warnings: c.warnings,
        }
      : { reason: cap.unresolved(row.model_id) }),
  }
}

// ── Builders ─────────────────────────────────────────────────────────────────
//
// Each builds PARTIAL rows carrying only what its own payload knows. None of
// them joins fit or cache any more: `mergeRows` folds them by model id and
// `decorate` applies both joins once, to the merged row. Doing it per builder
// meant five joins producing five rows for one model, each with a third of the
// story.

function base(modelId: string, label: string, where: Facet): ModelRow {
  return {
    ...EMPTY,
    key: modelId,
    model_id: modelId,
    label,
    where: [where],
    servedNames: [],
  }
}

export function catalogRows(catalog: CuratedModel[] | null | undefined): ModelRow[] {
  return (catalog ?? []).map((m) => ({
    ...base(m.model_id, m.label, 'catalog'),
    detail: m.detail,
    default_context: m.default_context,
    default_concurrency: m.default_concurrency,
  }))
}

/** What is already on the cluster's disks.
 *
 *  Built from the cache rather than from the catalogue, so a repo somebody
 *  pulled by hand appears without anything having curated it. */
export function onDeviceRows(cache: CacheIndex): ModelRow[] {
  return cache.all().map((entry) => base(entry.repo_id, entry.repo_id, 'ondisk'))
}

/** Hub search hits, and ONLY hub hits.
 *
 *  `/api/models/search` federates three sources and also returns the
 *  deployments and provider models it knows about. Those are dropped here,
 *  because `useCluster` and `useProviders` already carry the same models with
 *  strictly more on them -- node ids, health, context length, prices. Taking
 *  the search copy would let a row exist with a `provider` facet and an empty
 *  `providers[]`: a row that says somebody serves this and cannot say who. */
export function hubRows(hits: ModelSearchResponse | null | undefined): ModelRow[] {
  return (hits?.results ?? [])
    .filter((h) => h.origin === 'hub')
    .map((h) => ({
      ...base(h.model_id, h.model_id, 'hub'),
      downloads: h.downloads ?? null,
      likes: h.likes ?? null,
      gated: h.gated ?? null,
      tags: h.tags ?? [],
      pipelineTag: h.pipeline_tag ?? null,
      quantHint: h.quant_hint ?? null,
    }))
}

export function runningRows(cluster: Cluster | null | undefined): ModelRow[] {
  return (cluster?.deployments ?? []).map((d) => ({
    ...base(d.model_id, d.model_id, 'running'),
    servedNames: d.served_name ? [d.served_name] : [],
    deployments: [
      {
        deployment_id: d.deployment_id,
        served_name: d.served_name,
        state: d.state,
        runtime: d.runtime,
        node_ids: d.node_ids ?? [],
        last_error: d.last_error ?? null,
      },
    ],
  }))
}

export function providerRows(providers: Provider[] | null | undefined): ModelRow[] {
  const out: ModelRow[] = []
  for (const p of providers ?? []) {
    for (const m of p.models ?? []) {
      out.push({
        ...base(m.upstream_id, m.upstream_id, 'provider'),
        servedNames: m.served_name ? [m.served_name] : [],
        providers: [providerFact(p, m)],
      })
    }
  }
  return out
}

/** One provider's facts about one model. Read off the payload; nothing derived
 *  and, deliberately, nothing key-shaped. */
function providerFact(p: Provider, m: ProviderModel): RowProvider {
  return {
    provider_id: p.provider_id,
    display_name: p.display_name || p.provider_id,
    served_name: m.served_name,
    context_length: m.context_length ?? null,
    input_cost_per_mtok: m.input_cost_per_mtok,
    output_cost_per_mtok: m.output_cost_per_mtok,
    supports_tools: m.supports_tools,
    supports_streaming: m.supports_streaming,
    healthy: p.healthy,
    last_error: p.last_error,
    admitting: p.admitting,
    admission_block: p.admission_block,
  }
}

/** What the providers publish and are NOT serving.
 *
 *  The source of the `Not served` band, and the one place in this app that
 *  reads an unfiltered provider surface. Everything else -- `/api/providers`,
 *  `/v1/models`, `/api/topology` -- carries only enabled models, filtered
 *  server side; `GET /api/providers/{id}/models` is unfiltered precisely
 *  because it is the endpoint the allowlist is chosen from.
 *
 *  Rows with `enabled` are dropped rather than merged: `providerRows` has
 *  already produced them off the filtered listing, with health and admission
 *  on them, which this endpoint does not carry.
 *
 *  Keyed on `upstream_id` exactly, never case-folded, for the reason
 *  `mergeRows` states: OpenRouter spells things `qwen/qwen3-30b-a3b` where the
 *  hub spells them `Qwen/Qwen3-30B-A3B`, and folding would give an un-served
 *  remote row a local verdict for weights it is not the same thing as. */
export function offeredRows(
  catalogues: Record<string, ProviderCatalogueModel[]> | null | undefined,
  providers: Provider[] | null | undefined,
): ModelRow[] {
  const named = new Map(
    (providers ?? []).map((p) => [p.provider_id, p.display_name || p.provider_id]),
  )
  const out: ModelRow[] = []
  for (const [providerId, models] of Object.entries(catalogues ?? {})) {
    for (const m of models ?? []) {
      if (m.enabled) continue
      out.push({
        ...base(m.upstream_id, m.upstream_id, 'offered'),
        offers: [
          {
            provider_id: providerId,
            display_name: named.get(providerId) ?? providerId,
            served_name: m.served_name,
            upstream_id: m.upstream_id,
            context_length: m.context_length ?? null,
            input_cost_per_mtok: m.input_cost_per_mtok,
            output_cost_per_mtok: m.output_cost_per_mtok,
            supports_tools: m.supports_tools,
            supports_streaming: m.supports_streaming,
          },
        ],
      })
    }
  }
  return out
}

/** Every provider that serves this exact model id. Exported for the detail
 *  pane, which needs it for a model the list never produced a row for. */
export function providerRowsFor(
  providers: Provider[] | null | undefined,
  modelId: string,
): RowProvider[] {
  const out: RowProvider[] = []
  for (const p of providers ?? []) {
    for (const m of p.models ?? []) {
      if (m.upstream_id === modelId) out.push(providerFact(p, m))
    }
  }
  return out
}

// ── Merge ────────────────────────────────────────────────────────────────────

/** Fold the builders' partial rows into one row per model.
 *
 *  Keyed on the model id EXACTLY, never case-folded. OpenRouter spells things
 *  `qwen/qwen3-30b-a3b` where HuggingFace spells them `Qwen/Qwen3-30B-A3B`,
 *  and folding case would merge two ids into one row that inherits a local
 *  `fits` verdict for weights the provider may not be serving. That is an
 *  identity inference, and this UI does not invent. The cost is a visible
 *  near-duplicate, which is the honest picture.
 *
 *  `lists` arrive in FACET_ORDER: local, polled, structured facts first, so a
 *  search payload can only ever fill a gap rather than overwrite an answer. */
/** The registry payload as rows.
 *
 *  One row per payload row: nothing folded and nothing invented. `where`
 *  arrives canonical, deduplicated and non-empty, so this copies it rather
 *  than re-deriving it -- the fold that used to happen here now happens once,
 *  server side, over the five sources at one instant instead of over five
 *  polls on different intervals.
 *
 *  `remoteOnly` and `unservedOnly` ARE derived here, from `where`, because
 *  `band()` reads them and two answers to "is this provider-only" would put
 *  the band and the pane at odds. */
export function registryRows(
  reg: ModelRegistryResponse | null | undefined,
): ModelRow[] {
  return (reg?.models ?? []).map((m) => ({
    ...EMPTY,
    key: m.model_id,
    model_id: m.model_id,
    label: m.label,
    where: m.where as Facet[],
    servedNames: m.served_names,
    detail: m.detail,
    ...(m.default_context != null ? { default_context: m.default_context } : {}),
    ...(m.default_concurrency != null
      ? { default_concurrency: m.default_concurrency }
      : {}),
    deployments: m.deployments,
    providers: m.providers,
    offers: m.offers,
    cachedOn: m.cached_on,
    bytesOnDisk: m.bytes_on_disk,
    remoteOnly: m.where.length === 1 && m.where[0] === 'provider',
    unservedOnly: m.where.length === 1 && m.where[0] === 'offered',
  }))
}

/** Fold hub search hits onto the registry rows.
 *
 *  All that survives of `mergeRows`, and only this half survives because
 *  `/api/models/search` is the one source the registry deliberately does not
 *  hold: it resolves nothing, so its hits are a query's answer rather than a
 *  fact about this cluster.
 *
 *  A hit for a model the registry already knows FILLS GAPS and nothing else.
 *  It contributes no deployment, no provider and no offer -- it has none --
 *  and it never overwrites a label, because a curated label is what a human
 *  wrote and `compare()` sorts on it.
 *
 *  Keyed on `model_id` exactly, never case-folded, for the reason `mergeRows`
 *  gave and which is still true: OpenRouter spells things
 *  `qwen/qwen3-30b-a3b` where the hub spells them `Qwen/Qwen3-30B-A3B`, and
 *  folding would give an un-served remote row a local verdict for weights it
 *  is not the same thing as. */
export function withHubHits(
  rows: ModelRow[],
  hits: ModelSearchResponse | null | undefined,
): ModelRow[] {
  const byId = new Map(rows.map((r) => [r.model_id, r]))
  for (const h of hubRows(hits)) {
    const prev = byId.get(h.model_id)
    if (!prev) {
      byId.set(h.model_id, h)
      continue
    }
    byId.set(h.model_id, {
      ...prev,
      // 'hub' is appended rather than canonicalised in: the payload's `where`
      // is already in FACET_ORDER and 'hub' is last in it.
      where: prev.where.includes('hub') ? prev.where : [...prev.where, 'hub'],
      downloads: prev.downloads ?? h.downloads,
      likes: prev.likes ?? h.likes,
      gated: prev.gated ?? h.gated,
      tags: prev.tags.length ? prev.tags : h.tags,
      pipelineTag: prev.pipelineTag ?? h.pipelineTag,
      quantHint: prev.quantHint ?? h.quantHint,
    })
  }
  return [...byId.values()]
}

export function mergeRows(lists: ModelRow[][]): ModelRow[] {
  const out = new Map<string, ModelRow>()
  for (const list of lists) {
    for (const row of list) {
      const prev = out.get(row.model_id)
      if (!prev) {
        out.set(row.model_id, { ...row, key: row.model_id })
        continue
      }
      out.set(row.model_id, {
        ...prev,
        where: [...prev.where, ...row.where],
        // A curated label is the only one worth preferring: it is what a human
        // wrote. Otherwise the model id, never a served name -- `compare()`
        // sorts on the label, and a served name that differs from the repo id
        // would file the row somewhere surprising.
        label: row.where.includes('catalog') ? row.label : prev.label,
        detail: prev.detail || row.detail,
        default_context: prev.default_context ?? row.default_context,
        default_concurrency: prev.default_concurrency ?? row.default_concurrency,
        servedNames: [...prev.servedNames, ...row.servedNames],
        deployments: [...prev.deployments, ...row.deployments],
        providers: [...prev.providers, ...row.providers],
        offers: [...prev.offers, ...row.offers],
        // Only the hub builder sets these, so "first non-null" is really
        // "whichever contributor had them".
        downloads: prev.downloads ?? row.downloads,
        likes: prev.likes ?? row.likes,
        gated: prev.gated ?? row.gated,
        tags: prev.tags.length ? prev.tags : row.tags,
        pipelineTag: prev.pipelineTag ?? row.pipelineTag,
        quantHint: prev.quantHint ?? row.quantHint,
      })
    }
  }

  for (const row of out.values()) {
    const seen = new Set(row.where)
    row.where = FACET_ORDER.filter((f) => seen.has(f))
    row.servedNames = [...new Set(row.servedNames)].sort()
    row.remoteOnly = row.where.length === 1 && row.where[0] === 'provider'
    row.unservedOnly = row.where.length === 1 && row.where[0] === 'offered'
    // A provider that has since switched this model on contributes to BOTH
    // arrays for one poll, because the two endpoints are fetched on different
    // intervals. Being served is the stronger fact and wins: without this the
    // row would keep an offer for something it is already serving, and the
    // pane would draw a Serve button beside a Stop serving one.
    if (row.providers.length) {
      const served = new Set(row.providers.map((p) => p.provider_id))
      row.offers = row.offers.filter((o) => !served.has(o.provider_id))
    }
  }
  return [...out.values()]
}

/** Apply the two joins every row needs, once, after the merge.
 *
 *  `checking` is set only for a model that is BOTH in flight and still without
 *  an answer, so a row can never be both "checking" and carrying a verdict --
 *  the flag drops the instant the batch lands. */
export function decorate(
  rows: ModelRow[],
  cap: CapacityIndex,
  cache: CacheIndex,
  checking: ReadonlySet<string> = new Set(),
): ModelRow[] {
  return rows.map((r) => ({
    ...withFit(r, cap, cache),
    checking: checking.has(r.model_id) && !cap.row(r.model_id),
  }))
}

// ── Banding, filtering, sorting ──────────────────────────────────────────────

export function band(row: ModelRow): Band {
  // Reality outranks a prediction, and this is not a hypothetical: on this
  // cluster `openai/gpt-oss-120b` is SERVING RIGHT NOW and the capacity walk
  // at 8192/1 refuses it -- because the live budget already has this
  // deployment's own memory taken out of it. Filing a model that is answering
  // requests under "Needs more memory" would be flatly false.
  //
  // A finished deployment does not count: it bands by its verdict and says
  // what went wrong on the row instead.
  if (row.deployments.some((d) => !TERMINAL.has(d.state))) return 'running'
  // Nothing serves this and nothing here has it: it exists in the list only
  // because a provider publishes it. Tested before the verdict rather than
  // after, so a stray answer from a batch cannot lift a price-sheet row up
  // among the models this cluster actually runs.
  if (row.unservedOnly) return 'unserved'
  switch (row.verdict) {
    case 'fits':
      return 'fits'
    case 'fits_degraded':
      return 'degraded'
    case 'wont_fit':
      return 'wont'
    default:
      // Nothing local can resolve a provider-only model, so "not checked"
      // would promise a verdict that is never coming.
      return row.remoteOnly ? 'elsewhere' : 'unchecked'
  }
}

/** Deployment state as a lamp signal. The rule `QuantLadder` already applies,
 *  exported so the list and the ladder cannot disagree about what `degraded`
 *  looks like. */
export function deploymentSignal(state: DeploymentState): 'live' | 'warn' | 'fault' {
  if (state === 'ready') return 'live'
  if (TERMINAL.has(state)) return 'fault'
  return 'warn'
}

export const BAND_TITLE: Record<Band, string> = {
  running: 'Running here',
  fits: 'Fits here',
  degraded: 'Loads, but decode is slow',
  unchecked: 'Not checked',
  wont: 'Needs more memory',
  elsewhere: 'Runs on a provider, not here',
  unserved: 'Not served',
}

/** The order bands draw in.
 *
 *  Measurement first, then predictions best-to-worst, then the absence of one,
 *  then the refusal. `elsewhere` sits last, below even a refusal: a refusal is
 *  a statement about THIS hardware and a provider model was never a candidate
 *  for it. Keeping the top of the list local is what makes one flat list
 *  readable when a provider publishes several hundred models. */
const BAND_ORDER: Band[] = [
  'running',
  'fits',
  'degraded',
  'unchecked',
  'wont',
  'elsewhere',
  // Below even `elsewhere`, which is at least something somebody chose to
  // serve. This band is a vendor's catalogue, and its whole design is to be
  // one collapsed line at the bottom of the screen.
  'unserved',
]

/** The only band that draws collapsed.
 *
 *  A provider publishes several hundred models and this cluster serves the few
 *  somebody picked. The rest belong on the screen -- they are one click from
 *  being served, and hiding them entirely is what stopped this tab being where
 *  you choose -- but they are a vendor's catalogue, not an answer to "what runs
 *  here", and expanded they would bury every band above them.
 *
 *  Pure, and here rather than in the component, so `rows.check.mjs` can hold it
 *  to the rule: exactly one band hides itself, and it is the one with no local
 *  verdict in it. */
export function isCollapsibleBand(band: Band): boolean {
  return band === 'unserved'
}

/** Who publishes the models in a band, in words.
 *
 *  Named rather than counted, the way the capacity caption names machines:
 *  "428 from OpenRouter" is something somebody can check, and "428 from your
 *  providers" is not. Null for every band that is not about offers. */
export function bandSubtitle(group: Group): string | null {
  if (group.band !== 'unserved') return null
  const names = [
    ...new Set(group.rows.flatMap((r) => r.offers.map((o) => o.display_name))),
  ].sort()
  if (!names.length) return null
  if (names.length === 1) return `from ${names[0]}`
  return `from ${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`
}

/** The descriptive half of a catalogue row, in order, as plain strings.
 *
 *  Out here rather than inside `CatalogList.tsx` for the reason `ladder.ts`
 *  and `speculative.ts` are: `rows.check.mjs` bundles with esbuild's
 *  `platform: 'neutral'` and cannot import a module that pulls in React, so a
 *  decision left in the component is a decision nothing can check. This one
 *  went unchecked and was wrong -- see the runtime clause below.
 *
 *  Strings only. `gated` and the cache pill stay in the component because they
 *  are styled spans, not facts. */
export function rowFacts(row: ModelRow): string[] {
  const parts: string[] = []
  if (row.detail) parts.push(row.detail)
  // ARITHMETIC, not an offer. `capacity._walk` steps down
  // `QUANT_SUGGESTION_ORDER` and answers which scheme WOULD hold -- it never
  // asks whether anybody published the model at that scheme, so "requantized
  // to q2_k" can name a build that does not exist. The real offer is the
  // variant ladder on the model's own page, where every row is a repository
  // that was sized; `CatalogList` links there. So this says what it is.
  if (row.requantized && row.dtype) parts.push(`would fit at ${row.dtype}`)
  if (row.quantHint) parts.push(row.quantHint)
  if (row.pipelineTag) parts.push(row.pipelineTag)
  // One label per RUNNING runtime, distinct -- never one per deployment
  // record. The server keeps terminal deployments deliberately (a FAILED one
  // is still the answer to "what happened to this model"), so a crash-looping
  // model carries hundreds of them: `gpt-oss-20b` reached 200 and printed
  // "vllm" two hundred times into one subtitle. Distinct, and live, because a
  // row with nothing running should say nothing here -- it still bands by its
  // verdict and still says what went wrong. Named rather than counted, the
  // same rule `bandSubtitle` above states.
  for (const runtime of [
    ...new Set(
      row.deployments
        .filter((d) => !TERMINAL.has(d.state))
        .map((d) => d.runtime)
        .filter(Boolean),
    ),
  ].sort())
    parts.push(runtime)
  // Who publishes it without serving it, and on what terms. The price is the
  // whole of what switching it on costs, so it belongs on the row rather than
  // only behind a click. Null prices print as "not priced", never as $0 -- the
  // wire keeps "never published a price" and "free" apart on purpose.
  for (const o of row.offers) {
    parts.push(
      o.input_cost_per_mtok != null && o.output_cost_per_mtok != null
        ? `${o.display_name} · $${o.input_cost_per_mtok.toFixed(2)} / $${o.output_cost_per_mtok.toFixed(2)} per Mtok`
        : `${o.display_name} · not priced`,
    )
  }
  if (row.downloads != null) parts.push(`${row.downloads.toLocaleString()} downloads`)
  if (row.likes != null && row.likes > 0) parts.push(`${row.likes.toLocaleString()} likes`)
  return parts
}

export type Sort = 'fit' | 'size' | 'downloads' | 'name'

export const SORTS: { id: Sort; label: string }[] = [
  { id: 'fit', label: 'fit' },
  { id: 'size', label: 'size' },
  { id: 'downloads', label: 'downloads' },
  { id: 'name', label: 'name' },
]

/** Sort within a band. Never across one -- the band is the answer, and a sort
 *  that reorders a "will not fit" above a "fits" would be a second opinion on
 *  the question the fit gate already settled. */
function compare(a: ModelRow, b: ModelRow, sort: Sort): number {
  switch (sort) {
    case 'size': {
      // Largest first: at equal fit, more parameters is the better model, and
      // it is the same tie-break the variant ladder uses.
      //
      // Parameters are compared against parameters ONLY. This used to fall
      // back to `bytesOnDisk`, which put a 120e9-parameter count and a 195e9
      // byte count in one comparison as though they were one quantity --
      // invisible while each source was its own list, obvious the moment forty
      // cached repos and four curated models share a band. On-disk size breaks
      // ties only among rows that have no measured parameter count.
      const av = a.total_params ?? -1
      const bv = b.total_params ?? -1
      if (av !== bv) return bv - av
      if (av < 0) {
        const ab = a.bytesOnDisk ?? -1
        const bb = b.bytesOnDisk ?? -1
        if (ab !== bb) return bb - ab
      }
      break
    }
    case 'downloads': {
      const av = a.downloads ?? -1
      const bv = b.downloads ?? -1
      if (av !== bv) return bv - av
      break
    }
    case 'fit': {
      // Already banded; inside a band, something on disk beats something that
      // has to be pulled first.
      const av = a.cachedOn.length ? 0 : 1
      const bv = b.cachedOn.length ? 0 : 1
      if (av !== bv) return av - bv
      break
    }
    case 'name':
      break
  }
  return a.label.localeCompare(b.label)
}

export interface Group {
  title: string
  band: Band
  rows: ModelRow[]
}

/** Rows into the sections the list draws.
 *
 *  Bands first, then the source's own grouping inside a band that has no fit
 *  meaning (running, providers), so a deployment list still reads by publisher
 *  rather than as one undifferentiated block. */
export function groupRows(rows: ModelRow[], sort: Sort): Group[] {
  const byBand = new Map<Band, ModelRow[]>()
  for (const r of rows) {
    const b = band(r)
    const bucket = byBand.get(b)
    if (bucket) bucket.push(r)
    else byBand.set(b, [r])
  }

  const out: Group[] = []
  for (const b of BAND_ORDER) {
    const bucket = byBand.get(b)
    if (!bucket?.length) continue
    out.push({
      title: BAND_TITLE[b],
      band: b,
      rows: [...bucket].sort((x, y) => compare(x, y, sort)),
    })
  }
  return out
}

/** Substring match over everything the row actually knows.
 *
 *  The old filter read `model_id` and `label` only, so typing "MoE" or "mxfp4"
 *  -- both of which are printed on the row -- matched nothing. */
export function matches(row: ModelRow, needle: string): boolean {
  if (!needle) return true
  const n = needle.toLowerCase()
  return (
    row.model_id.toLowerCase().includes(n) ||
    row.label.toLowerCase().includes(n) ||
    row.detail.toLowerCase().includes(n) ||
    (row.dtype?.toLowerCase().includes(n) ?? false) ||
    (row.quantHint?.toLowerCase().includes(n) ?? false) ||
    (row.pipelineTag?.toLowerCase().includes(n) ?? false) ||
    row.tags.some((t) => t.toLowerCase().includes(n)) ||
    // The name you would actually send as `model` at /v1. Typing
    // "gpt-oss-120b" must find the repo it is served from.
    row.servedNames.some((s) => s.toLowerCase().includes(n)) ||
    // "openrouter" should find everything OpenRouter serves.
    row.providers.some(
      (p) =>
        p.display_name.toLowerCase().includes(n) ||
        p.provider_id.toLowerCase().includes(n),
    ) ||
    // ...including the ones it publishes and does not serve, which is how a
    // search reaches inside the collapsed band without expanding it.
    row.offers.some(
      (o) =>
        o.display_name.toLowerCase().includes(n) ||
        o.provider_id.toLowerCase().includes(n) ||
        o.served_name.toLowerCase().includes(n),
    )
  )
}

// ── Format ───────────────────────────────────────────────────────────────────

/** Studio's format dropdown, in derate's terms.
 *
 *  Theirs offers GGUF and Checkpoint because llama.cpp makes both real. Here
 *  GGUF is the format nothing loads, so the filter earns its place for the
 *  opposite reason: it is how somebody looks at only the rows a runtime here
 *  could actually take, or only the ones it could not. */
export type Format = 'all' | 'checkpoint' | 'gguf'

export const FORMATS: { id: Format; label: string }[] = [
  { id: 'all', label: 'all formats' },
  { id: 'checkpoint', label: 'checkpoint' },
  { id: 'gguf', label: 'GGUF' },
]

/** Whether a row is GGUF, from the only signals a listing carries: the tags
 *  the hub returned and the name the publisher chose. A guess, and it drives a
 *  filter rather than a verdict. */
export function isGguf(row: ModelRow): boolean {
  if (row.tags.some((t) => t.toLowerCase() === 'gguf')) return true
  const hay = `${row.model_id} ${row.quantHint ?? ''} ${row.dtype ?? ''}`.toLowerCase()
  return hay.includes('gguf')
}

export function matchesFormat(row: ModelRow, format: Format): boolean {
  if (format === 'all') return true
  return format === 'gguf' ? isGguf(row) : !isGguf(row)
}
