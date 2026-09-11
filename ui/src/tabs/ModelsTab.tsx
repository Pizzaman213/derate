import { useEffect, useMemo, useRef, useState } from 'react'
import {
  useCapacity,
  useProviderCatalogues,
  useProviderKinds,
  useModelRegistry,
  useProviders,
  useQuantTable,
  useStorage,
} from '../state/resources'
import { useBackend } from '../state/backend'
import { useRouter } from '../state/router'
import { usePlacement } from '../state/placement'
import type { CapacityReport, ModelSearchResponse } from '../api/types'
import { QuantTableCard } from './models/QuantTableCard'
import { PullCard } from './models/PullCard'
import { pullableProviders } from './models/pullTargets'
import { CatalogList } from './models/CatalogList'
import { CardGrid } from './models/CardGrid'
import { InstalledModelsCard } from './models/InstalledModelsCard'
import { ModelInspector } from './models/ModelInspector'
import { UnchosenBanner } from './models/UnchosenBanner'
import { CustomServesCard } from './models/CustomServes'
import type { CustomServe } from '../state/customServes'
import { Select, type SelectOption } from '../components/Select'
import {
  cacheIndex,
  registryRows,
  capacityIndex,
  decorate,
  withHubHits,
  groupRows,
  matches,
  SORTS,
  FORMATS,
  matchesFormat,
  type Format,
  type ModelRow,
  type Sort,
} from './models/rows'

const VIEW_KEY = 'derate.models.view'

/** How many verdict-less rows one settled question is allowed to resolve.
 *
 *  Each one is a hub round trip on a cold cache, so this is a real cost, not a
 *  render budget. Ten is about a screenful: the rows somebody is actually
 *  looking at get a real answer and everything below them keeps saying "not
 *  checked", which is true. Raising it does not make the screen better, it
 *  makes the hub angrier. */
const AUTO_CHECK = 10

const EMPTY_SET: ReadonlySet<string> = new Set()

type View = 'cards' | 'rows'

/** Models: what you could run here, and what each quantization of it costs.
 *
 *  ONE list. It used to be five mutually-exclusive source tabs -- Recommended,
 *  On device, Hub, Running, Providers -- and the split cost the screen the
 *  question a picker exists to answer: "who can serve me this model, and can I
 *  run it myself?" You had to know which tab a model lived in before you could
 *  look for it, and searching only ever reached the hub, and only while the
 *  Hub tab happened to be open.
 *
 *  So the sources became facets of one row. A model that is curated, already
 *  on disk AND serving right now is one row that says all three, not three
 *  rows each saying a third of it. `GET /api/models` folds them by model id
 *  -- server side, over all five sources at one instant, rather than in the
 *  browser over five polls on five intervals -- and `decorate` joins fit and
 *  cache once, afterwards.
 *
 *  Rows are banded by the fit gate's verdict -- running, fits, loads-but-slow,
 *  unchecked, will not, runs-elsewhere -- so the top of the screen is the part
 *  that actually runs. The bands come from `GET /api/capacity`; nothing here
 *  recomputes a fit.
 *
 *  Two things still carry no verdict, and both say so rather than guessing.
 *  A hub hit nobody has resolved has no fit answer at all, so the first ten
 *  such rows are resolved in one batched request as the list settles and the
 *  rest keep a hollow lamp. A provider-only model has no local answer that
 *  could ever exist -- it is somebody else's hardware -- so it bands last and
 *  the pane explains why instead of the row implying a verdict is coming. */
export function ModelsTab() {
  const registry = useModelRegistry()
  const providers = useProviders()
  // Mounted once here for the same reason `providers` is: `useResource` does
  // not deduplicate, so a copy in the inspector and another in the pull card
  // would be two intervals asking for one static table. Both need it -- the
  // ladder to know which providers can be pulled onto, the card to say the
  // same thing -- and neither may answer differently.
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

  const { route, navigate } = useRouter()
  // The machines ticked on the Serve panel's board, so the list bands against
  // the same hardware the ladder sizes against. Null -- the default -- is the
  // coordinator's own host.
  const { nodeIds } = usePlacement()

  const [query, setQuery] = useState('')
  // Nobody is asked for these any more.
  //
  // This screen used to carry a Context and a Seqs field above the list, before
  // a model had been chosen, and the Serve panel carried the same pair again.
  // Two fields writing one piece of state read as two settings that might
  // disagree, and both of them asked for a number in the units of a thing the
  // person had not picked yet. They are gone: the coordinator derives a context
  // per model from what actually fits, clamped to that model's own window, and
  // an override lives behind the Serve panel's advanced disclosure.
  //
  // They stay in the URL for the reason they were put there -- the question a
  // verdict answers has to travel with the verdict, or a link to a model at 8k
  // opens at 128k for the person you sent it to -- but null now means "the
  // coordinator picked", which is a different request from `?ctx=8192`.
  // Null is the default path and means "the coordinator picks", per model,
  // from what actually fits. Nothing on this screen sets them any more -- the
  // only way to a non-null value is the Serve panel's advanced disclosure, or
  // a link somebody shared that already had one.
  const context = route.context
  const concurrency = route.concurrency
  const [sort, setSort] = useState<Sort>('fit')
  const [format, setFormat] = useState<Format>('all')
  /** The model in the detail pane. Not the shell's sheet: it is this screen's
   *  own selection and it has to survive every list change around it.
   *
   *  It is the path -- `/models/meta-llama/Llama-3.1-8B` -- because it is the
   *  subject of the screen rather than a setting on it, and because that URL
   *  plus the two numbers above is the whole of what somebody means when they
   *  send you a model.
   *
   *  The id only. It used to snapshot context and concurrency at the moment of
   *  opening, which meant editing either field afterwards rebanded the list
   *  while the pane beside it went on answering the old question -- the caption
   *  saying 32,768 and the ladder under it saying 8,192, both correct, on one
   *  screen. The numbers now come from one place for both. */
  const selected = route.model
  // Seeds the Verdict card's custom-command field for the model the
  // custom-serves box just opened. Component state, not URL: it names a
  // one-time choice made on the way in, not a fact about the model that a
  // shared link should carry -- unlike `context` and `concurrency` above.
  const [pendingCustomCommand, setPendingCustomCommand] = useState<string | undefined>(undefined)
  // Cards to browse, rows to compare. Remembered, because which one somebody
  // wants depends on what they came here to do and that does not change
  // between visits.
  const [view, setView] = useState<View>(
    () => (window.localStorage.getItem(VIEW_KEY) as View | null) ?? 'cards',
  )
  const quantTable = useQuantTable()

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
  // One sequence for the whole tab, bumped on every change including the
  // early return -- an answer for a query the field no longer holds must not
  // land. Same pattern, and same reason, as ServePanel's plan effect.
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
  // specifically wins over a walk that happened to include it. Batches taken
  // at other numbers are dropped rather than shown under the wrong caption.
  const cap = useMemo(
    () =>
      capacityIndex(
        capacity.data,
        ...(batch.key === capKey ? batch.reports : []),
      ),
    [capacity.data, batch, capKey],
  )

  // One search, always live. It used to fire only while the Hub tab was open,
  // which meant the one box on the screen searched five different things
  // depending on a tab you had to have chosen first.
  useEffect(() => {
    const mine = ++seq.current
    const needle = query.trim()
    if (!needle) {
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
        .searchModels(needle)
        .then((r) => {
          if (seq.current !== mine) return
          setHits(r)
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
  }, [backend, query])

  // One feed, one list. The fold that used to happen here -- five builders
  // over five payloads polled on five different intervals -- happens once now,
  // server side, over all five sources at a single instant. That closes a skew
  // this file used to patch over: `/api/providers` (15s) and
  // `/api/providers/{id}/models` (300s) disagreed for one tick after somebody
  // switched a model on, and the row claimed to be both served and merely
  // offered.
  //
  // Hub hits still fold in here, and only here, because `/api/models/search`
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

  const groups = useMemo(() => {
    const needle = query.trim()
    // Local rows filter instantly. A row the hub returned is kept whatever the
    // box now says: the gateway already narrowed those hits to a `model_id`
    // substring of the query IT was given, and while the next search is in
    // flight the answer on screen belongs to the previous needle. Re-filtering
    // would blank the hub's whole contribution for 450ms on every keystroke.
    let kept = needle
      ? rows.filter((r) => r.where.includes('hub') || matches(r, needle))
      : rows
    if (format !== 'all') kept = kept.filter((r) => matchesFormat(r, format))
    return groupRows(kept, sort)
  }, [rows, query, sort, format])

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
  const needle = query.trim()
  // Not the raw box: while a search is in flight the top rows are the PREVIOUS
  // answer's, and a batch spent on them buys verdicts for a list about to be
  // replaced.
  const settled = needle === '' || (hits != null && hits.query === needle)
  const batchKey = settled ? `${needle}\u0000${capKey}\u0000${format}` : null

  useEffect(() => {
    if (batchKey == null || asked.current === batchKey) return
    const ids = groups
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
  }, [batchKey, groups, backend, capContext, capConcurrency, capKey])

  // A feed failing greys nothing and empties nothing: it prints one line
  // naming the feed and the server's own sentence above the list, and the
  // others still answer. Only a total blackout -- every feed failed AND there
  // is not one row to show -- is handed to the list as an error, because that
  // is the only state where the list itself has nothing to say.
  //
  // Folding the sources server side moved most of those feeds inside one
  // request, so the per-feed sentences now arrive in its `sources` block
  // rather than as four failed fetches. The rule is unchanged; only where
  // the sentence comes from is.
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
  // sentence is the server's own -- named per feed, exactly as when this
  // screen made the four requests itself.
  for (const [name, src] of Object.entries(registry.data?.sources ?? {})) {
    if (!src.ok && src.reason) problems.push({ name, message: src.reason })
  }
  // One provider's catalogue failing costs that provider's un-served rows and
  // nothing else -- the hook degrades per provider rather than as a whole -- so
  // it is named here beside the other feeds rather than blanking the list.
  for (const f of catalogues.data?.failed ?? []) {
    problems.push({ name: `${f.provider_id} catalogue`, message: f.message })
  }
  if (searchError) problems.push({ name: 'huggingface', message: searchError })
  if (batchError) problems.push({ name: 'capacity', message: batchError })

  const loading = rows.length === 0 && (feeds.some((f) => f.r.loading) || searching)
  const fatal =
    rows.length === 0 && problems.length >= feeds.length
      ? new Error(problems.map((p) => `${p.name}: ${p.message}`).join('\n'))
      : null

  /** Adopt the numbers a curated entry is normally served at, so the ladder's
   *  verdicts are taken at something sensible rather than at whatever was last
   *  typed into the fields above.
   *
   *  Opens the detail pane beside the list rather than a sheet over it.
   *  Choosing a model is comparison work -- read a verdict, go back, read the
   *  next -- and a modal that covers the list makes you hold the previous
   *  answer in your head. */
  const open = (row: ModelRow) => {
    // A normal open is never a replay of a custom serve; without this, a
    // custom serve opened earlier this session would silently seed a model
    // that has nothing to do with it.
    setPendingCustomCommand(undefined)
    // One navigation, not three: the model and the numbers it is being judged
    // at are one state, and pushing them separately would put two intermediate
    // URLs in the history for a single click.
    navigate({
      dest: 'models',
      model: row.model_id,
      ...(row.default_context ? { context: row.default_context } : {}),
      ...(row.default_concurrency ? { concurrency: row.default_concurrency } : {}),
    })
  }

  // Reopens the model a custom serve was launched on, with its command
  // ready to replay. No `default_context`/`default_concurrency` to adopt --
  // this is not a catalogue row -- so the coordinator picks, exactly as it
  // does for a model opened cold.
  const openCustomServe = (serve: CustomServe) => {
    setPendingCustomCommand(serve.command)
    navigate({ dest: 'models', model: serve.modelId })
  }

  return (
    <div style={{ display: 'grid', gap: 'var(--s-4)' }}>
      <div className="card2">
        <h3>Models</h3>
        <div className="unit">
          What could run here, and what each quantization of it would cost. Every
          verdict comes from the fit gate a launch goes through, so nothing on
          this screen can promise something the launch would refuse.
        </div>

        {/* The pill toolbar: search, the two filters, the sort, and the view
            toggle on the right. Context and sequences stay as plain fields --
            they are the question every verdict is answered at, not a filter,
            and dressing them as one would hide that. */}
        <div className="mbar">
          <div className="mbar-search">
            <span className="mbar-icon" aria-hidden>
              &#8981;
            </span>
            <input
              id="mt-q"
              aria-label="Search models"
              value={query}
              placeholder="Search models, on device and on the hub"
              spellCheck={false}
              onChange={(e) => setQuery(e.target.value)}
            />
            {query ? (
              <button
                type="button"
                className="mbar-clear"
                aria-label="Clear search"
                onClick={() => setQuery('')}
              >
                &#10005;
              </button>
            ) : null}
          </div>

          <Select
            aria-label="Format filter"
            value={format}
            options={FORMATS.map((f): SelectOption<Format> => ({ value: f.id, label: f.label }))}
            onChange={setFormat}
          />

          <Select
            aria-label="Sort models"
            value={sort}
            options={SORTS.map((s): SelectOption<Sort> => ({ value: s.id, label: s.label }))}
            onChange={setSort}
          />

          <div className="mview" role="radiogroup" aria-label="View">
            {(['cards', 'rows'] as View[]).map((v) => (
              <button
                key={v}
                type="button"
                role="radio"
                aria-checked={view === v}
                aria-pressed={view === v}
                onClick={() => {
                  setView(v)
                  window.localStorage.setItem(VIEW_KEY, v)
                }}
              >
                <span aria-hidden>{v === 'cards' ? '\u25a6' : '\u25a4'}</span>
                <span className="sr-only">{v}</span>
                {v === 'cards' && selected ? (
                  <span className="sr-only"> (browse view; the open model shows rows)</span>
                ) : null}
              </button>
            ))}
          </div>
        </div>

      </div>

      {/* Full width until something is open. The split is for comparing one
          model against the list; with nothing selected it was just a 360px
          column holding fifty cards one per row, which is the narrowest
          possible way to show a catalogue. */}
      <div className={selected ? 'msplit detail' : 'msplit'}>
        <div className="card2 msplit-list" id="mt-panel">
          {/* A provider serving its whole catalogue because nobody ever chose.
              Above the caption, because it is a statement about which of these
              rows are on the API rather than about how they were judged. */}
          <UnchosenBanner providers={providers.data} />

          <CustomServesCard onReuse={openCustomServe} />

          {/* Where the verdicts came from, said once at the top rather than
              repeated on every row.

              Read off the REPORT, never off anything this screen holds. There
              are no fields here any more, but the rule is older than that and
              is the reason there are none: a caption built from what was asked
              names a context the verdicts beneath it were not taken at, for as
              long as the answer takes to arrive.

              `context` is null when nobody named one, which is the default
              path. The gate then chose per model -- so the caption says which
              machine and which budget, and each row carries its own number. */}
          <p className="unit" style={{ margin: '0 0 6px' }}>
            {capacity.data && cap.basis
              ? `Fit on ${machinesPhrase(capacity.data)}, ` +
                (capacity.data.context
                  ? `at ${capacity.data.context.toLocaleString()} context and ` +
                    `${capacity.data.concurrency} ` +
                    `${capacity.data.concurrency === 1 ? 'sequence' : 'sequences'}, `
                  : 'each at the largest context it can hold up to its own window, ') +
                (capacity.data.budget_basis === 'host_memory'
                  ? capacity.data.local_serving === false
                    ? 'against host memory — no GPU was found, so nothing here can be ' +
                      'served from this machine. '
                    : 'against host memory — no GPU was found, so these are sized ' +
                      'for the CPU runtime. '
                  : cap.basis === 'live'
                    ? 'against what the nodes can hand out right now. '
                    : 'against the idle-hardware ceiling — there is no live memory reading. ') +
                `The first ${AUTO_CHECK} rows with no verdict are checked as the ` +
                'list settles; the rest say “not checked” rather than guessing.'
              : 'No capacity answer yet, so nothing here is banded by fit.'}
          </p>

          {/* A feed failing greys nothing and empties nothing. It says which
              feed, and why, in that server's own words. */}
          {problems.map((p) => (
            <p
              key={p.name}
              className="label"
              style={{
                fontWeight: 400,
                color: 'var(--warn)',
                whiteSpace: 'pre-wrap',
                margin: '0 0 6px',
              }}
            >
              {p.name}: {p.message}
            </p>
          ))}

          {hits ? (
            <>
              {Object.entries(hits.sources)
                .filter(([, v]) => !v.ok && v.note)
                .map(([name, v]) => (
                  <p
                    key={name}
                    className="label"
                    style={{
                      fontWeight: 400,
                      color: 'var(--warn)',
                      whiteSpace: 'pre-wrap',
                      margin: '0 0 6px',
                    }}
                  >
                    {name}: {v.note}
                  </p>
                ))}
              {hits.notes.map((n) => (
                <p key={n} className="unit" style={{ margin: '0 0 6px' }}>
                  {n}
                </p>
              ))}
            </>
          ) : null}

          {/* Cards to browse, rows to compare -- same rows, same banding, same
              verdicts underneath, only the shape of the thing you scan.

              With a model open the master pane is always rows, whatever the
              toggle says. A 204px card in a 360px column is one card per row
              and forty-eight of them is a column of postage stamps; Studio's
              split does the same thing for the same reason. The toggle governs
              browsing, which is where a card grid earns its space. */}
          <div className="mpanel">
            {view === 'cards' && !selected ? (
              <CardGrid
                groups={groups}
                table={quantTable.data}
                // Whether a GGUF row has anywhere to go. The catalogue's red
                // dot says nothing on this cluster launches llama.cpp, which
                // stays true -- but with a pullable provider configured it is
                // no longer the whole story, and the card is where somebody
                // decides not to open the model.
                canPull={pullTargets.length > 0}
                loading={loading}
                error={fatal}
                emptyNote={emptyNote(query)}
                onOpen={open}
                // A search has to reach inside the collapsed band, or the box
                // silently finds nothing in the several hundred rows it hides.
                expandAll={query.trim() !== ''}
              />
            ) : (
              <CatalogList
                groups={groups}
                loading={loading}
                error={fatal}
                emptyNote={emptyNote(query)}
                onOpen={open}
                selectedId={selected}
                compact={selected != null}
                expandAll={query.trim() !== ''}
              />
            )}
          </div>
        </div>

        {selected ? (
          <div className="card2 msplit-detail">
            <div className="msplit-detail-body">
              {/* Keyed on the id so switching models remounts rather than
                  letting the previous model's ladder linger under the new
                  heading while its own two requests are still in flight.

                  The debounced numbers, not the raw fields: these are the same
                  pair the capacity walk is asked for, so the bands in the list
                  and the ladder in this pane are always answering one question,
                  and typing a context does not fire a hub call per keystroke. */}
              <ModelInspector
                key={selected}
                modelId={selected}
                context={capContext}
                concurrency={capConcurrency}
                // Already polled for the list; handed down rather than polled
                // again, and it is what lets a provider-only model open.
                providers={providers.data}
                providerKinds={providerKinds.data}
                // The unfiltered catalogues, already polled for the list. The
                // Serve pane needs them for two things: which providers publish
                // this model without serving it, and -- the load-bearing one --
                // every OTHER model's current state, because the allowlist is
                // written as the complete set rather than a delta.
                catalogues={catalogues.data?.byProvider ?? null}
                catalogueErrors={catalogues.data?.failed ?? null}
                // Open on the routing side for a row that exists only because
                // a provider publishes it. There is nothing here to run, so
                // landing on "Run it here" would answer a question nobody
                // asked -- and this is the row somebody clicked precisely to
                // put the model on the API.
                preferRoute={
                  rows.find((r) => r.model_id === selected)?.unservedOnly === true
                }
                initialCustomCommand={pendingCustomCommand}
                // Replace, for the reason the sheet closes with a replace
                // (state/selection.tsx): the entry that opened this pane
                // becomes a pane-less one, so Back leaves the tab rather
                // than reopening what you just closed.
                onClose={() => navigate({ model: null }, { replace: true })}
              />
            </div>
          </div>
        ) : null}
      </div>

      <InstalledModelsCard
        storage={storage.data}
        loading={storage.loading}
        error={storage.error}
      />

      <PullCard providerKinds={providerKinds.data} />

      <QuantTableCard />
    </div>
  )
}

/** Which machines a report was taken on, in words.
 *
 *  Named rather than counted. "one machine" was the old apology's phrasing and
 *  it is exactly what somebody cannot check: the whole complaint was that the
 *  screen would not say WHICH. A degree above 1 is stated too, because two
 *  machines sharded and two machines considered separately are different
 *  answers with the same node list. */
function machinesPhrase(report: CapacityReport): string {
  const nodes = report.nodes ?? []
  const tp = Math.max(...(report.tensor_parallel ?? [1]), 1)
  if (nodes.length === 0 || !nodes[0]) return 'no machine'
  if (nodes.length === 1) return nodes[0]
  const listed = `${nodes.slice(0, -1).join(', ')} and ${nodes[nodes.length - 1]}`
  return tp > 1 ? `${listed}, tensor-parallel ${tp}` : listed
}

function emptyNote(query: string): string {
  const q = query.trim()
  if (q) return `Nothing here matches ${q}.`
  return 'Nothing is running, cached, curated, or published by a provider yet.'
}
