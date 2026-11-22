import { useEffect, useMemo, useRef, useState } from 'react'
import {
  useCapacity,
  useModelRegistry,
  useProviderCatalogues,
  useProviderKinds,
  useProviders,
  usePins,
  useProfiles,
  useStorage,
} from '../../state/resources'
import { useBackend } from '../../state/backend'
import { useRouter } from '../../state/router'
import { usePlacement } from '../../state/placement'
import type { CapacityReport, ModelSearchResponse } from '../../api/types'
import { pullableProviders } from './pullTargets'
import { parseQuery, rowPasses } from './query'
import {
  band,
  cacheIndex,
  capacityIndex,
  decorate,
  groupRows,
  matches,
  registryRows,
  withHubHits,
  type Band,
  type Group,
  type ModelRow,
  type Sort,
} from './rows'

/** How many verdict-less rows one settled question is allowed to resolve.
 *
 *  Each one is a hub round trip on a cold cache, so this is a real cost, not a
 *  render budget. Ten is about a screenful: the rows somebody is actually
 *  looking at get a real answer and everything below them keeps saying "not
 *  checked", which is true. Raising it does not make the screen better, it
 *  makes the hub angrier. */
export const AUTO_CHECK = 10

const EMPTY_SET: ReadonlySet<string> = new Set()

/** The ways to look at one list. Views, not sources: every row is still
 *  the one merged row `GET /api/models` folded, and a view only decides which
 *  of them this screen is about right now. `profiles` is the exception: it
 *  lists saved launch setups across every model, from `GET /api/profiles`. */
export type ListView = 'fits' | 'cluster' | 'hub' | 'providers' | 'profiles'

export const LIST_VIEWS: { id: ListView; label: string }[] = [
  { id: 'fits', label: 'Fits here' },
  { id: 'cluster', label: 'On this cluster' },
  { id: 'hub', label: 'Hugging Face' },
  { id: 'providers', label: 'Providers' },
  { id: 'profiles', label: 'Profiles' },
]

export function isListView(value: string | null): value is ListView {
  return LIST_VIEWS.some((v) => v.id === value)
}

/** The Hugging Face view's own narrowing, sent to the hub search. */
export interface HubFilters {
  /** `unsloth` for the Unsloth quants source; absent for original repos. */
  author?: string
  /** A pipeline tag, e.g. `text-generation`; absent for every task. */
  task?: string
}

const FITS_BANDS: Partial<Record<Band, true>> = { running: true, fits: true, degraded: true, notnow: true }

/** Not running, not on disk, not curated: the row exists because a search
 *  found it, so nothing on this cluster has touched it yet. */
function hubOnly(row: ModelRow): boolean {
  return !row.where.some((w) => w === 'running' || w === 'ondisk' || w === 'catalog')
}

/** Whether a row belongs to a view. `showWont` only widens `fits`: the other
 *  views are about where a model is, not whether it fits. */
function inView(row: ModelRow, view: ListView, showWont: boolean): boolean {
  switch (view) {
    case 'fits': {
      if (row.remoteOnly || row.unservedOnly) return false
      const b = band(row)
      if (FITS_BANDS[b]) return true
      if (b === 'wont') return showWont
      // An unchecked row is worth a place only while somebody is asking about
      // it, or when it is a model this cluster already chose to keep.
      if (b === 'unchecked')
        return row.checking || row.where.includes('catalog') || row.where.includes('ondisk')
      return false
    }
    case 'cluster':
      return row.where.includes('running') || row.where.includes('ondisk')
    case 'hub':
      return row.where.includes('hub') || row.where.includes('catalog')
    case 'providers': {
      const b = band(row)
      return (
        b === 'elsewhere' ||
        b === 'unserved' ||
        row.where.includes('provider') ||
        row.where.includes('offered')
      )
    }
    case 'profiles':
      return false
  }
}

/** The order a hub-only group draws in: what fits first, best to worst, then
 *  everything nobody has judged yet (index -1, sorted last below). */
const HUB_ORDER: Band[] = ['fits', 'degraded', 'notnow', 'wont']

/** Which machines a report was taken on, in words.
 *
 *  Named rather than counted. "one machine" was the old apology's phrasing and
 *  it is exactly what somebody cannot check: the whole complaint was that the
 *  screen would not say WHICH. A degree above 1 is stated too, because two
 *  machines sharded and two machines considered separately are different
 *  answers with the same node list. */
export function machinesPhrase(report: CapacityReport): string {
  const nodes = report.nodes ?? []
  const tp = Math.max(...(report.tensor_parallel ?? [1]), 1)
  if (nodes.length === 0 || !nodes[0]) return 'no machine'
  if (nodes.length === 1) return nodes[0]
  const listed = `${nodes.slice(0, -1).join(', ')} and ${nodes[nodes.length - 1]}`
  return tp > 1 ? `${listed}, tensor-parallel ${tp}` : listed
}

/** Models: what you could run here, and what each quantization of it costs.
 *
 *  ONE list. It used to be five mutually-exclusive source tabs, and the split
 *  cost the screen the question a picker exists to answer: "who can serve me
 *  this model, and can I run it myself?" So the sources became facets of one
 *  row, folded server side by `GET /api/models` over all five sources at one
 *  instant, and `decorate` joins fit and cache once, afterwards. The views
 *  below are lenses over that one list, never separate fetches.
 *
 *  Rows are banded by the fit gate's verdict, from `GET /api/capacity`;
 *  nothing here recomputes a fit. A hub hit nobody has resolved has no answer
 *  at all, so the first ten such rows on screen are resolved in one batched
 *  request as the list settles and the rest keep a hollow lamp. */
export function useModelList(
  query: string,
  sort: Sort,
  view: ListView,
  showWont: boolean,
  hubFilters: HubFilters = {},
) {
  const pinsRes = usePins()
  const pins = useMemo<ReadonlySet<string>>(() => new Set(pinsRes.data ?? []), [pinsRes.data])
  // Every profile, for the Profiles view and its count.
  const profiles = useProfiles(null)
  const registry = useModelRegistry()
  const providers = useProviders()
  // Mounted once here for the same reason `providers` is: `useResource` does
  // not deduplicate, so a copy in the pane and another in the pull card would
  // be two intervals asking for one static table.
  const providerKinds = useProviderKinds()
  const pullTargets = pullableProviders(providers.data, providerKinds.data)
  // What the providers publish and are NOT serving. A second call per provider,
  // and the only unfiltered provider read in the app: `/api/providers` above
  // carries just the models the allowlist lets through, which is right for
  // every other screen and is exactly why this tab could not be the place you
  // choose. Lazy -- a catalogue changes on a refresh or a pull, not on a timer.
  const catalogues = useProviderCatalogues(
    (providers.data ?? []).map((p) => p.provider_id),
  )
  const storage = useStorage()

  const { route } = useRouter()
  // The machines ticked on the detail pane's node bars, so the list bands
  // against the same hardware the ladder sizes against. Null -- the default --
  // is the coordinator's own choice.
  const { nodeIds } = usePlacement()

  // Null is the default path and means "the coordinator picks", per model,
  // from what actually fits. They stay in the URL because the question a
  // verdict answers has to travel with the verdict.
  const context = route.context
  const concurrency = route.concurrency

  // The box, read once. `words` is what gets searched and matched -- never the
  // raw text, or typing `fits` would send "fits" to the hub.
  const parsed = useMemo(() => parseQuery(query), [query])
  const words = parsed.words

  // Debounced before it becomes a request: the capacity walk resolves every
  // catalogue model against the hub, and firing one per keystroke while
  // somebody types "16384" would cost five of them.
  const [capContext, setCapContext] = useState(context)
  const [capConcurrency, setCapConcurrency] = useState(concurrency)
  const [capNodes, setCapNodes] = useState(nodeIds)
  useEffect(() => {
    const id = window.setTimeout(() => {
      setCapContext(context)
      setCapConcurrency(concurrency)
      setCapNodes(nodeIds)
    }, 450)
    return () => window.clearTimeout(id)
  }, [context, concurrency, nodeIds])

  const capacity = useCapacity(capContext, capConcurrency, capNodes)

  const { backend } = useBackend()
  const [hits, setHits] = useState<ModelSearchResponse | null>(null)
  const [searching, setSearching] = useState(false)
  const [searchError, setSearchError] = useState<string | null>(null)
  // One sequence for the whole list, bumped on every change including the
  // early return -- an answer for a query the field no longer holds must not
  // land.
  const seq = useRef(0)

  const cache = useMemo(() => cacheIndex(storage.data), [storage.data])
  // The whole question, so a batch taken at other numbers -- or on other
  // machines -- is dropped rather than shown under this caption.
  const capKey = `${capContext}/${capConcurrency}/${(capNodes ?? []).join(',')}`
  const [batch, setBatch] = useState<{ key: string; reports: CapacityReport[] }>({
    key: capKey,
    reports: [],
  })
  const [inflight, setInflight] = useState<ReadonlySet<string>>(EMPTY_SET)
  const [batchError, setBatchError] = useState<string | null>(null)
  const asked = useRef<string | null>(null)
  const batchSeq = useRef(0)

  // The 20s cluster-wide poll AND every targeted batch taken at these numbers,
  // folded into one index. Batches come last so a request that named a model
  // specifically wins over a walk that happened to include it.
  const cap = useMemo(
    () => capacityIndex(capacity.data, ...(batch.key === capKey ? batch.reports : [])),
    [capacity.data, batch, capKey],
  )

  // One search, always live, on the words alone -- plus, on the Hugging Face
  // view, its source, task and sort. That view searches even with an empty
  // box: it is a browse of the hub's own top models, not of this cluster.
  const author = view === 'hub' ? hubFilters.author : undefined
  const task = view === 'hub' ? hubFilters.task : undefined
  // An empty box under "Best fit" opens on what is NEW: the hub's trending
  // list, which is recent releases people are actually pulling. Its raw
  // newest-created list is not that -- minutes-old uploads with 0 downloads.
  const hubSort =
    view !== 'hub'
      ? undefined
      : sort === 'trending' || (sort === 'fit' && !words)
        ? 'trending'
        : sort === 'updated'
          ? 'updated'
          : 'downloads'
  // The whole question a hub answer belongs to: the same words under another
  // source or sort is a different listing.
  const searchKey = `${words}\u0000${author ?? ''}\u0000${task ?? ''}\u0000${hubSort ?? ''}`
  const [hitsKey, setHitsKey] = useState<string | null>(null)
  useEffect(() => {
    const mine = ++seq.current
    const needle = words
    if (!needle && view !== 'hub') {
      setHits(null)
      setSearchError(null)
      setSearching(false)
      return
    }
    setSearching(true)
    // An unauthenticated hub is rate limited and a person types faster than it
    // answers, so this waits rather than firing per keystroke.
    const timer = window.setTimeout(() => {
      backend
        .searchModels(needle, { author, task, ...(hubSort ? { sort: hubSort } : {}) })
        .then((r) => {
          if (seq.current !== mine) return
          setHits(r)
          setHitsKey(searchKey)
          setSearchError(null)
        })
        .catch((e: unknown) => {
          if (seq.current !== mine) return
          setHits(null)
          setSearchError(e instanceof Error ? e.message : String(e))
        })
        .finally(() => {
          if (seq.current === mine) setSearching(false)
        })
    }, 450)
    return () => window.clearTimeout(timer)
  }, [backend, words, author, task, hubSort, view, searchKey])

  // What the hub itself answered, by id. The Hugging Face view draws exactly
  // this -- the curated catalogue rows are this cluster's shortlist, not a
  // listing, and mixed in they buried what the hub actually has.
  const hubIds = useMemo(
    () => new Set((hits?.results ?? []).filter((h) => h.origin === 'hub').map((h) => h.model_id)),
    [hits],
  )

  // Hub hits fold in here, and only here, because `/api/models/search`
  // resolves nothing: its answers are a query's, not a fact about this
  // cluster. A hit can only ever fill a gap.
  const merged = useMemo<ModelRow[]>(
    () => withHubHits(registryRows(registry.data), hits),
    [registry.data, hits],
  )

  const rows = useMemo(
    () => decorate(merged, cap, cache, inflight),
    [merged, cap, cache, inflight],
  )

  // The query applied, before any view. A row the hub returned is kept
  // whatever the box now says: the gateway already narrowed those hits to the
  // words IT was given, and while the next search is in flight the answer on
  // screen belongs to the previous words. Re-filtering would blank the hub's
  // whole contribution for 450ms on every keystroke.
  const queried = useMemo(() => {
    let kept = words ? rows.filter((r) => r.where.includes('hub') || matches(r, words)) : rows
    for (const f of parsed.filters) kept = kept.filter((r) => rowPasses(r, f, { pins }))
    return kept
  }, [rows, words, parsed.filters, pins])

  const { groups, pinnedGroup, hubOnlyGroup, wontCount } = useMemo(() => {
    let viewRows: ModelRow[]
    let hubGroup: Group | null = null
    if (view === 'fits' && words) {
      // A search on the fits view reaches past this cluster, so what it found
      // there is drawn apart rather than mixed into the bands above it: those
      // rows say what fits, these say what the hub has that might.
      const local: ModelRow[] = []
      const found: ModelRow[] = []
      for (const r of queried) {
        if (r.remoteOnly || r.unservedOnly) continue
        if (hubOnly(r)) {
          if (band(r) !== 'wont' || showWont) found.push(r)
        } else if (inView(r, view, showWont)) {
          local.push(r)
        }
      }
      viewRows = local
      if (found.length) {
        hubGroup = {
          title: 'On Hugging Face',
          band: 'unchecked',
          rows: found
            .map((r, i) => {
              const rank = HUB_ORDER.indexOf(band(r))
              return { r, i, rank: rank < 0 ? HUB_ORDER.length : rank }
            })
            .sort((a, b) => a.rank - b.rank || a.i - b.i)
            .map((x) => x.r),
        }
      }
    } else {
      viewRows =
        view === 'hub'
          ? queried.filter((r) => hubIds.has(r.model_id))
          : queried.filter((r) => inView(r, view, showWont))
    }
    const wont = queried.filter(
      (r) => !r.remoteOnly && !r.unservedOnly && band(r) === 'wont',
    ).length
    // Pinned rows lead, in their own group, and leave their band's group.
    const pinnedRows = view === 'hub' ? [] : viewRows.filter((r) => pins.has(r.model_id))
    const rest = pinnedRows.length ? viewRows.filter((r) => !pins.has(r.model_id)) : viewRows
    const pinned: Group | null = pinnedRows[0]
      ? { title: 'Pinned', band: band(pinnedRows[0]), rows: pinnedRows }
      : null
    return {
      // On the hub view "Best fit" still groups by verdict, but inside a group
      // it is the listing the hub was asked for, not the alphabet.
      groups: groupRows(rest, view === 'hub' && sort === 'fit' ? (hubSort ?? 'downloads') : sort),
      pinnedGroup: pinned,
      hubOnlyGroup: hubGroup,
      wontCount: wont,
    }
  }, [queried, view, words, showWont, sort, pins, hubIds, hubSort])

  const counts = useMemo(() => {
    const out = { fits: 0, cluster: 0, hub: 0, providers: 0, profiles: 0 } as Record<ListView, number>
    for (const r of rows) for (const v of LIST_VIEWS) if (inView(r, v.id, false)) out[v.id] += 1
    out.hub = hubIds.size
    out.profiles = profiles.data?.length ?? 0
    return out
  }, [rows, profiles.data, hubIds])

  // Models nothing here has touched that the gate says would run now, by how
  // many people pulled them. A suggestion, ordered by a fact on the wire.
  const worthTrying = useMemo(
    () =>
      rows
        .filter(
          (r) =>
            !r.where.includes('running') &&
            !r.where.includes('ondisk') &&
            (band(r) === 'fits' || band(r) === 'degraded'),
        )
        .sort((a, b) => (b.downloads ?? -1) - (a.downloads ?? -1))
        .slice(0, 6),
    [rows],
  )

  // ── The auto-check ────────────────────────────────────────────────────────
  //
  // A searched model has no verdict, because `/api/capacity` walks the curated
  // shortlist and nothing else. A list whose every row says "not checked" is
  // not answering the question it exists for, so the first screenful of
  // verdict-less rows is resolved in ONE batched request.
  //
  // The hard part is termination. The trigger is one batch per SETTLED
  // question -- not "there exist unchecked rows", which would re-fire for ever
  // as each batch lands and the next ten rows surface.
  //
  // Not the raw box: while a search is in flight the top rows are the PREVIOUS
  // answer's, and a batch spent on them buys verdicts for a list about to be
  // replaced.
  const settled =
    (words === '' && view !== 'hub') || (hits != null && hitsKey === searchKey)
  const batchKey = settled ? `${query.trim()}\u0000${searchKey}\u0000${capKey}\u0000${view}` : null

  useEffect(() => {
    if (batchKey == null || asked.current === batchKey) return
    const ids = [...(pinnedGroup ? [pinnedGroup] : []), ...groups, ...(hubOnlyGroup ? [hubOnlyGroup] : [])]
      .flatMap((g) => g.rows)
      // `unservedOnly` is excluded for the same reason `remoteOnly` is, and
      // it matters more: those rows are verdict-less by nature and there can
      // be several hundred of them, so without this every settle would spend
      // the whole batch resolving a vendor's catalogue against the hub.
      .filter((r) => r.verdict == null && !r.remoteOnly && !r.unservedOnly && !r.checking)
      .slice(0, AUTO_CHECK)
      .map((r) => r.model_id)
    // Claimed BEFORE the request, and even when there is nothing to ask, so a
    // failure or an empty answer cannot loop either.
    asked.current = batchKey
    if (!ids.length) return

    const mine = ++batchSeq.current
    setInflight(new Set(ids))
    setBatchError(null)
    backend
      .capacityFor(ids, capContext, capConcurrency)
      .then((report) => {
        if (batchSeq.current !== mine) return
        setBatch((b) =>
          // Keyed on the numbers it was taken at. A verdict taken at 8k is not
          // an answer about 128k, and keeping it would put a stale band under
          // a caption naming the new numbers.
          b.key === capKey
            ? { key: capKey, reports: [...b.reports, report] }
            : { key: capKey, reports: [report] },
        )
      })
      .catch((e: unknown) => {
        if (batchSeq.current !== mine) return
        setBatchError(e instanceof Error ? e.message : String(e))
      })
      .finally(() => {
        if (batchSeq.current === mine) setInflight(EMPTY_SET)
      })
  }, [batchKey, groups, pinnedGroup, hubOnlyGroup, backend, capContext, capConcurrency, capKey])

  // A feed failing greys nothing and empties nothing: it prints one line
  // naming the feed and the server's own sentence above the list, and the
  // others still answer. Only a total blackout -- every feed failed AND there
  // is not one row to show -- is handed to the list as an error, because that
  // is the only state where the list itself has nothing to say.
  const feeds = [
    { name: 'models', r: registry },
    { name: 'storage', r: storage },
    { name: 'providers', r: providers },
  ]
  const problems: { name: string; message: string }[] = []
  for (const f of feeds) {
    if (f.r.error) problems.push({ name: f.name, message: f.r.error.message })
  }
  // The list arrives folded, so a source that failed server side leaves no
  // failed fetch here to notice. It says so in the payload instead, and each
  // sentence is the server's own.
  for (const [name, src] of Object.entries(registry.data?.sources ?? {})) {
    if (!src.ok && src.reason) problems.push({ name, message: src.reason })
  }
  // One provider's catalogue failing costs that provider's un-served rows and
  // nothing else, so it is named here beside the other feeds.
  for (const f of catalogues.data?.failed ?? []) {
    problems.push({ name: `${f.provider_id} catalogue`, message: f.message })
  }
  if (searchError) problems.push({ name: 'huggingface', message: searchError })
  else if (hits?.sources.huggingface?.ok === false && hits.sources.huggingface.note)
    problems.push({ name: 'huggingface', message: hits.sources.huggingface.note })
  if (batchError) problems.push({ name: 'capacity', message: batchError })

  const loading = rows.length === 0 && (feeds.some((f) => f.r.loading) || searching)
  const fatal =
    rows.length === 0 && problems.length >= feeds.length
      ? new Error(problems.map((p) => `${p.name}: ${p.message}`).join('\n'))
      : null

  return {
    parsed,
    rows,
    groups,
    pinnedGroup,
    hubOnlyGroup,
    pins,
    pinsRes,
    profiles,
    counts,
    worthTrying,
    wontCount,
    capacity,
    cap,
    capContext,
    capConcurrency,
    providers,
    providerKinds,
    catalogues,
    pullTargets,
    storage,
    hits,
    searching,
    problems,
    loading,
    fatal,
  }
}
