import type {
  CapacityReport,
  CapacityRow,
  Cluster,
  CuratedModel,
  ModelSearchResponse,
  Provider,
  StorageReport,
  Verdict,
} from '../../api/types'

/** Where a row came from. These are the tabs, and they are sources rather than
 *  fit bands: a model that does not fit is still a model you were looking for,
 *  and hiding it behind a verdict makes the catalogue look smaller than it is.
 *  Fit sorts and groups *within* a source instead. */
export type Origin = 'catalog' | 'ondevice' | 'hub' | 'running' | 'providers'

/** The band a row draws under inside its source.
 *
 *  `unchecked` is not a fourth verdict, it is the absence of one:
 *  `GET /api/models/search` never resolves anything (an asserted contract, not
 *  an implementation detail), so a hub row genuinely has no fit answer. Drawing
 *  it as a guess would be inventing the one number this UI is not allowed to
 *  invent. */
export type Band = 'fits' | 'degraded' | 'wont' | 'unchecked' | 'plain'

/** One model, with the facts kept apart instead of pre-joined into a string.
 *
 *  The list this feeds used to receive `{label, detail, group}` with `detail`
 *  already concatenated, which is why it could never badge fit, sort by size,
 *  or say what was on disk. Every field below is either read from the wire or
 *  joined from another payload by exact key -- nothing here is computed from a
 *  model's name or its bytes. */
export interface ModelRow {
  /** Stable across refetches: source plus id, since the same model can appear
   *  under more than one source and React needs them distinct. */
  key: string
  model_id: string
  label: string
  origin: Origin
  /** Section heading within the source. */
  group: string

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
  /** The quantization the capacity walk had to step down to, when it stepped. */
  dtype: string | null
  requantized: boolean
  warnings: string[]

  // ── On disk. Joined from `/api/storage` by repo id. ───────────────────────
  cachedOn: string[]
  bytesOnDisk: number | null

  // ── Hub facts that were on the type and rendered nowhere. ─────────────────
  downloads: number | null
  likes: number | null
  gated: boolean | string | null
  tags: string[]
  pipelineTag: string | null
  quantHint: string | null

  /** The curated one-liner. Only catalog rows have one. */
  detail: string
  default_context?: number
  default_concurrency?: number

  /** Served by somebody else's hardware: there is nothing here to resolve it
   *  against, so it opens nothing. */
  remote: boolean
  /** Deployment state, for rows that are already running. */
  state: string | null
  runtime: string | null
}

const EMPTY = {
  verdict: null,
  reason: null,
  basis: null,
  predicted_decode_tps: null,
  headroom: null,
  total_params: null,
  dtype: null,
  requantized: false,
  warnings: [] as string[],
  cachedOn: [] as string[],
  bytesOnDisk: null,
  downloads: null,
  likes: null,
  gated: null,
  tags: [] as string[],
  pipelineTag: null,
  quantHint: null,
  detail: '',
  remote: false,
  state: null,
  runtime: null,
} satisfies Omit<ModelRow, 'key' | 'model_id' | 'label' | 'origin' | 'group'>

// ── Joins ────────────────────────────────────────────────────────────────────

export interface CapacityIndex {
  row(modelId: string): CapacityRow | null
  basis: 'live' | 'static' | null
  /** Why a model in the catalogue has no verdict at all, in the walk's words. */
  unresolved(modelId: string): string | null
}

/** Index the capacity walk by model id.
 *
 *  Prefers the live side, because "what can I launch right now" is the question
 *  a picker is being asked; falls back to the static ceiling when the live
 *  figure is unavailable, and says which it used rather than presenting one as
 *  the other. A cold coordinator with no live reading still gets verdicts. */
export function capacityIndex(report: CapacityReport | null | undefined): CapacityIndex {
  const side = report?.live ?? report?.static ?? null
  const basis = report?.live ? 'live' : report?.static ? 'static' : null
  const byId = new Map<string, CapacityRow>()
  for (const r of side?.rows ?? []) byId.set(r.model_id, r)
  const unresolved = new Map<string, string>()
  for (const u of report?.unresolved ?? []) unresolved.set(u.model_id, u.reason)
  return {
    row: (id) => byId.get(id) ?? null,
    basis,
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
          basis: cap.basis,
          predicted_decode_tps: c.predicted_decode_tps,
          headroom: c.headroom,
          total_params: c.total_params,
          dtype: c.dtype,
          requantized: c.requantized,
          warnings: c.warnings,
        }
      : { reason: cap.unresolved(row.model_id) }),
  }
}

// ── Builders, one per source ─────────────────────────────────────────────────

/** Group by publisher-ish family so the list reads as sections rather than a
 *  flat wall. Purely presentational -- nothing downstream depends on it. */
export function family(modelId: string): string {
  const name = modelId.split('/').pop() ?? modelId
  const first = name.split(/[-_.]/)[0] ?? name
  return first.charAt(0).toUpperCase() + first.slice(1)
}

export function catalogRows(
  catalog: CuratedModel[] | null | undefined,
  cap: CapacityIndex,
  cache: CacheIndex,
): ModelRow[] {
  return (catalog ?? []).map((m) =>
    withFit(
      {
        ...EMPTY,
        key: `catalog::${m.model_id}`,
        model_id: m.model_id,
        label: m.label,
        origin: 'catalog',
        group: family(m.model_id),
        detail: m.detail,
        default_context: m.default_context,
        default_concurrency: m.default_concurrency,
      },
      cap,
      cache,
    ),
  )
}

/** What is already on the cluster's disks.
 *
 *  Built from the cache rather than from the catalogue, so a repo somebody
 *  pulled by hand appears here without anything having curated it. A row that
 *  also happens to be a catalogue model picks up its verdict on the way
 *  through; one that does not stays honest about having no verdict. */
export function onDeviceRows(cap: CapacityIndex, cache: CacheIndex): ModelRow[] {
  return cache.all().map((entry) =>
    withFit(
      {
        ...EMPTY,
        key: `ondevice::${entry.repo_id}`,
        model_id: entry.repo_id,
        label: entry.repo_id,
        origin: 'ondevice',
        group: entry.nodes.length === 1 ? `on ${entry.nodes[0]}` : `on ${entry.nodes.length} nodes`,
      },
      cap,
      cache,
    ),
  )
}

export function hubRows(hits: ModelSearchResponse | null, cache: CacheIndex): ModelRow[] {
  return (hits?.results ?? []).map((h) => ({
    ...EMPTY,
    key: `hub::${h.origin}::${h.model_id}`,
    model_id: h.model_id,
    label: h.model_id,
    origin: 'hub' as const,
    group: h.model_id.split('/')[0] ?? 'HuggingFace',
    downloads: h.downloads ?? null,
    likes: h.likes ?? null,
    gated: h.gated ?? null,
    tags: h.tags ?? [],
    pipelineTag: h.pipeline_tag ?? null,
    quantHint: h.quant_hint ?? null,
    // Deliberately no verdict: search does not resolve, so there is nothing to
    // read. What IS known is whether it is already downloaded, which is a
    // fact about our own disks and costs nothing.
    cachedOn: cache.nodes(h.model_id),
    bytesOnDisk: cache.bytes(h.model_id),
  }))
}

export function runningRows(cluster: Cluster | null | undefined, cache: CacheIndex): ModelRow[] {
  return (cluster?.deployments ?? []).map((d) => ({
    ...EMPTY,
    key: `running::${d.deployment_id}`,
    model_id: d.model_id,
    label: d.served_name,
    origin: 'running' as const,
    group: 'Running here',
    state: d.state,
    runtime: d.runtime,
    cachedOn: cache.nodes(d.model_id),
    bytesOnDisk: cache.bytes(d.model_id),
  }))
}

export function providerRows(providers: Provider[] | null | undefined): ModelRow[] {
  const out: ModelRow[] = []
  for (const p of providers ?? []) {
    for (const m of p.models ?? []) {
      out.push({
        ...EMPTY,
        key: `providers::${p.provider_id}::${m.served_name}`,
        model_id: m.upstream_id,
        label: m.served_name,
        origin: 'providers',
        group: p.display_name || p.provider_id,
        detail: m.context_length ? `${m.context_length.toLocaleString()} ctx` : '',
        remote: true,
      })
    }
  }
  return out
}

// ── Banding, filtering, sorting ──────────────────────────────────────────────

export function band(row: ModelRow): Band {
  if (row.origin === 'running' || row.origin === 'providers') return 'plain'
  switch (row.verdict) {
    case 'fits':
      return 'fits'
    case 'fits_degraded':
      return 'degraded'
    case 'wont_fit':
      return 'wont'
    default:
      return 'unchecked'
  }
}

export const BAND_TITLE: Record<Band, string> = {
  fits: 'Fits here',
  degraded: 'Loads, but decode is slow',
  wont: 'Needs more memory',
  unchecked: 'Not checked',
  plain: '',
}

/** The order bands draw in. Best answer first, absence of an answer last. */
const BAND_ORDER: Band[] = ['fits', 'degraded', 'unchecked', 'wont', 'plain']

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
      const av = a.total_params ?? a.bytesOnDisk ?? -1
      const bv = b.total_params ?? b.bytesOnDisk ?? -1
      if (av !== bv) return bv - av
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
    if (b === 'plain') {
      // No fit answer applies, so fall back to the source's own headings.
      const bySource = new Map<string, ModelRow[]>()
      for (const r of bucket) {
        const g = bySource.get(r.group)
        if (g) g.push(r)
        else bySource.set(r.group, [r])
      }
      for (const [title, group] of bySource) {
        out.push({ title, band: b, rows: [...group].sort((x, y) => compare(x, y, sort)) })
      }
      continue
    }
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
    row.tags.some((t) => t.toLowerCase().includes(n))
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
