import { useEffect, useMemo, useRef, useState } from 'react'
import {
  useCapacity,
  useCatalog,
  useCluster,
  useProviders,
  useQuantTable,
  useStorage,
} from '../state/resources'
import { useBackend } from '../state/backend'
import { DEFAULT_CONCURRENCY, DEFAULT_CONTEXT, useRouter } from '../state/router'
import type { ModelSearchResponse } from '../api/types'
import { QuantTableCard } from './models/QuantTableCard'
import { PullCard } from './models/PullCard'
import { CatalogList } from './models/CatalogList'
import { CardGrid } from './models/CardGrid'
import { ModelInspector } from './models/ModelInspector'
import {
  cacheIndex,
  capacityIndex,
  catalogRows,
  groupRows,
  hubRows,
  matches,
  onDeviceRows,
  providerRows,
  runningRows,
  SORTS,
  FORMATS,
  matchesFormat,
  type Format,
  type ModelRow,
  type Origin,
  type Sort,
} from './models/rows'

const SOURCES: { id: Origin; label: string }[] = [
  { id: 'catalog', label: 'Recommended' },
  { id: 'ondevice', label: 'On device' },
  { id: 'hub', label: 'Hub' },
  { id: 'running', label: 'Running' },
  { id: 'providers', label: 'Providers' },
]

const SOURCE_KEY = 'derate.models.source'
const VIEW_KEY = 'derate.models.view'

type View = 'cards' | 'rows'

/** Models: what you could run here, and what each quantization of it costs.
 *
 *  Five sources, not one firehose, and each opens on a populated list rather
 *  than an empty search box. Inside a source the rows are banded by the fit
 *  gate's verdict -- fits, then loads-but-slow, then unchecked, then won't --
 *  so the top of the screen is the part that actually runs. The bands are read
 *  from `GET /api/capacity`; nothing here recomputes a fit.
 *
 *  Two sources deliberately carry no verdict. Hub rows come from
 *  `GET /api/models/search`, which never resolves anything, so guessing a size
 *  from the repository name would be inventing the one number this screen is
 *  not allowed to invent -- they draw a hollow lamp and say "not checked"
 *  instead. Provider rows are somebody else's hardware and open nothing.
 *
 *  Picking a model opens the one sheet, where the quantization ladder lives.
 *  That costs the ability to compare a variant against the list behind it,
 *  which is the accepted trade for not inventing a second modal host. */
export function ModelsTab() {
  const catalog = useCatalog()
  const cluster = useCluster()
  const providers = useProviders()
  const storage = useStorage()

  const { route, navigate } = useRouter()

  const [source, setSource] = useState<Origin>('catalog')
  const [query, setQuery] = useState('')
  // 8192/1 matches what `GET /api/capacity` and the client's own fallback use.
  // The three surfaces asking this question used to default differently, which
  // meant the list and the dashboard could disagree about a model's fit while
  // both were right about their own numbers.
  //
  // In the URL rather than in component state, because the question this screen
  // answers is "does it fit *at these numbers*" -- a link to a model at 8k that
  // opens at 128k for the person you sent it to is a link to a different
  // answer. `?ctx=`/`?seq=` are omitted while they are at the default, so the
  // ordinary URL stays `/models`.
  const context = route.context ?? DEFAULT_CONTEXT
  const concurrency = route.concurrency ?? DEFAULT_CONCURRENCY
  // Replace, not push: this is typing. Back must leave the models tab, not
  // walk back through 1, 16, 163, 1638, 16384.
  const setContext = (n: number) => navigate({ context: n }, { replace: true })
  const setConcurrency = (n: number) => navigate({ concurrency: n }, { replace: true })
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
  useEffect(() => {
    const id = window.setTimeout(() => {
      setCapContext(context)
      setCapConcurrency(concurrency)
    }, 450)
    return () => window.clearTimeout(id)
  }, [context, concurrency])

  const capacity = useCapacity(capContext, capConcurrency)

  const { backend } = useBackend()
  const [hits, setHits] = useState<ModelSearchResponse | null>(null)
  const [searching, setSearching] = useState(false)
  const [searchError, setSearchError] = useState<string | null>(null)
  // One sequence for the whole tab, bumped on every change including the
  // early return -- an answer for a query the field no longer holds must not
  // land. Same pattern, and same reason, as PlannerBar.
  const seq = useRef(0)

  const cache = useMemo(() => cacheIndex(storage.data), [storage.data])
  const cap = useMemo(() => capacityIndex(capacity.data), [capacity.data])

  // First run opens on whichever source has something in it: On device when
  // the cluster has already pulled weights, the curated list otherwise. After
  // that the last-used source wins, because the source someone chose is a
  // stronger signal than anything this can infer.
  const restored = useRef(false)
  useEffect(() => {
    if (restored.current) return
    const saved = window.localStorage.getItem(SOURCE_KEY)
    if (saved && SOURCES.some((s) => s.id === saved)) {
      setSource(saved as Origin)
      restored.current = true
      return
    }
    // Wait for the first storage answer before deciding; guessing before it
    // lands would flip the tab out from under someone a second after opening.
    if (storage.loading || !storage.data) return
    if (cache.all().length) setSource('ondevice')
    restored.current = true
  }, [storage.loading, storage.data, cache])

  const pick = (next: Origin) => {
    setSource(next)
    restored.current = true
    window.localStorage.setItem(SOURCE_KEY, next)
  }

  useEffect(() => {
    if (source !== 'hub') return
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
  }, [backend, source, query])

  const rows = useMemo<ModelRow[]>(() => {
    switch (source) {
      case 'hub':
        return hubRows(hits, cache)
      case 'ondevice':
        return onDeviceRows(cap, cache)
      case 'running':
        return runningRows(cluster.data, cache)
      case 'providers':
        return providerRows(providers.data)
      default:
        return catalogRows(catalog.data, cap, cache)
    }
  }, [source, catalog.data, cluster.data, providers.data, hits, cap, cache])

  const groups = useMemo(() => {
    // The hub search already applied the query; filtering again would drop
    // rows the hub matched on a field the row does not show.
    const needle = source === 'hub' ? '' : query.trim()
    let kept = needle ? rows.filter((r) => matches(r, needle)) : rows
    if (format !== 'all') kept = kept.filter((r) => matchesFormat(r, format))
    return groupRows(kept, sort)
  }, [rows, query, source, sort, format])

  const status =
    source === 'catalog'
      ? catalog
      : source === 'ondevice'
        ? storage
        : source === 'hub'
          ? { loading: searching, error: searchError ? new Error(searchError) : null }
          : source === 'running'
            ? cluster
            : providers

  /** Adopt the numbers a curated entry is normally served at, so the ladder's
   *  verdicts are taken at something sensible rather than at whatever was last
   *  typed into the fields above.
   *
   *  Opens the detail pane beside the list rather than a sheet over it.
   *  Choosing a model is comparison work -- read a verdict, go back, read the
   *  next -- and a modal that covers the list makes you hold the previous
   *  answer in your head. */
  const open = (row: ModelRow) => {
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

  return (
    <div style={{ display: 'grid', gap: 'var(--s-4)' }}>
      <div className="card2">
        <h3>Models</h3>
        <div className="unit">
          What could run here, and what each quantization of it would cost. Every
          verdict comes from the fit gate a launch goes through, so nothing on
          this screen can promise something the launch would refuse.
        </div>

        <div className="chips" role="tablist" aria-label="Model source" style={{ marginTop: 12 }}>
          {SOURCES.map((s) => (
            <button
              key={s.id}
              id={`mt-tab-${s.id}`}
              role="tab"
              type="button"
              aria-selected={source === s.id}
              aria-pressed={source === s.id}
              aria-controls="mt-panel"
              onClick={() => pick(s.id)}
            >
              {s.label}
            </button>
          ))}
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
              placeholder={source === 'hub' ? 'Search HuggingFace' : 'Search models'}
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

          <select
            aria-label="Format filter"
            value={format}
            onChange={(e) => setFormat(e.target.value as Format)}
          >
            {FORMATS.map((f) => (
              <option key={f.id} value={f.id}>
                {f.label}
              </option>
            ))}
          </select>

          <select
            aria-label="Sort models"
            value={sort}
            onChange={(e) => setSort(e.target.value as Sort)}
          >
            {SORTS.map((s) => (
              <option key={s.id} value={s.id}>
                {s.label}
              </option>
            ))}
          </select>

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

        {/* Hidden while a model is open: the Serve panel in the detail pane
            carries the same pair, bound to the same two query parameters, and
            two identical fields writing one piece of state reads as two
            settings that might disagree. The caption above still states the
            numbers, so the list never stops saying what it is answering at. */}
        {/* Rendered away rather than `hidden`: `.bararea` sets `display:
            flex`, which beats the `hidden` attribute's UA rule, so the
            attribute alone would leave both pairs on screen. */}
        {selected ? null : (
        <div className="bararea">
          <div className="fld" style={{ width: 96 }}>
            <label htmlFor="mt-ctx">Context</label>
            <input
              id="mt-ctx"
              className="mono"
              type="number"
              min={1}
              value={context}
              onChange={(e) => {
                const n = Number(e.target.value)
                if (Number.isFinite(n) && n > 0) setContext(Math.round(n))
              }}
            />
          </div>
          <div className="fld" style={{ width: 72 }}>
            <label htmlFor="mt-seq">Seqs</label>
            <input
              id="mt-seq"
              className="mono"
              type="number"
              min={1}
              value={concurrency}
              onChange={(e) => {
                const n = Number(e.target.value)
                if (Number.isFinite(n) && n > 0) setConcurrency(Math.round(n))
              }}
            />
          </div>
        </div>
        )}
      </div>

      {/* Full width until something is open. The split is for comparing one
          model against the list; with nothing selected it was just a 360px
          column holding fifty cards one per row, which is the narrowest
          possible way to show a catalogue. */}
      <div className={selected ? 'msplit detail' : 'msplit'}>
        <div className="card2 msplit-list" id="mt-panel" role="tabpanel" aria-labelledby={`mt-tab-${source}`}>
          {/* Where the verdicts came from, said once at the top rather than
              repeated on every row. */}
          {source === 'catalog' || source === 'ondevice' ? (
            // The numbers come from the report, not from the fields above it.
            // A caption built from the inputs would name a context the verdicts
            // beneath it were not taken at, every time one of them changed.
            <p className="unit" style={{ margin: '0 0 6px' }}>
              {capacity.data && cap.basis
                ? `Fit at ${capacity.data.context.toLocaleString()} context and ` +
                  `${capacity.data.concurrency} ` +
                  `${capacity.data.concurrency === 1 ? 'sequence' : 'sequences'}, ` +
                  (cap.basis === 'live'
                    ? 'against what the nodes can hand out right now.'
                    : 'against the idle-hardware ceiling — there is no live memory reading.')
                : 'No capacity answer yet, so nothing here is banded by fit.'}
            </p>
          ) : null}

          {/* The hub failing greys one source; it does not empty the screen,
              and it says why in the resolver's own words. */}
          {source === 'hub' && hits ? (
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
                loading={status.loading}
                error={status.error}
                emptyNote={emptyNote(source, query)}
                onOpen={open}
              />
            ) : (
              <CatalogList
                groups={groups}
                loading={status.loading}
                error={status.error}
                emptyNote={emptyNote(source, query)}
                onOpen={open}
                selectedId={selected}
                compact={selected != null}
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

      <PullCard />

      <QuantTableCard />
    </div>
  )
}

function emptyNote(source: Origin, query: string): string {
  if (source === 'hub' && !query.trim()) return 'Type to search HuggingFace.'
  if (query.trim()) return `Nothing here matches ${query.trim()}.`
  switch (source) {
    case 'ondevice':
      return 'No weights are cached on any node yet. A model downloads on its first launch.'
    case 'running':
      return 'Nothing is deployed yet.'
    case 'providers':
      return 'No provider has published a model list.'
    default:
      return 'The catalog is empty.'
  }
}
