import { useEffect, useMemo, useRef, useState } from 'react'
import { useCapacity, useCatalog, useCluster, useProviders, useStorage } from '../state/resources'
import { useBackend } from '../state/backend'
import type { ModelSearchResponse } from '../api/types'
import { useSelection } from '../state/selection'
import { QuantTableCard } from './models/QuantTableCard'
import { CatalogList } from './models/CatalogList'
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
  const { openSheet } = useSelection()

  const [source, setSource] = useState<Origin>('catalog')
  const [query, setQuery] = useState('')
  // 8192/1 matches what `GET /api/capacity` and the client's own fallback use.
  // The three surfaces asking this question used to default differently, which
  // meant the list and the dashboard could disagree about a model's fit while
  // both were right about their own numbers.
  const [context, setContext] = useState(8192)
  const [concurrency, setConcurrency] = useState(1)
  const [sort, setSort] = useState<Sort>('fit')

  // Debounced before it becomes a request: the capacity walk resolves every
  // catalogue model against the hub, and firing one per keystroke while
  // somebody types "16384" would cost five of them.
  const [capContext, setCapContext] = useState(8192)
  const [capConcurrency, setCapConcurrency] = useState(1)
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
    return groupRows(
      needle ? rows.filter((r) => matches(r, needle)) : rows,
      sort,
    )
  }, [rows, query, source, sort])

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

        <div className="bararea">
          <div className="fld" style={{ flex: 1, minWidth: 200 }}>
            <label htmlFor="mt-q">Search</label>
            <input
              id="mt-q"
              value={query}
              placeholder={source === 'hub' ? 'search HuggingFace' : 'name, id, quantization or tag'}
              spellCheck={false}
              onChange={(e) => setQuery(e.target.value)}
            />
          </div>
          <div className="fld">
            <label htmlFor="mt-sort">Sort</label>
            <select id="mt-sort" value={sort} onChange={(e) => setSort(e.target.value as Sort)}>
              {SORTS.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.label}
                </option>
              ))}
            </select>
          </div>
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

        <div id="mt-panel" role="tabpanel" aria-labelledby={`mt-tab-${source}`}>
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

          <CatalogList
            groups={groups}
            loading={status.loading}
            error={status.error}
            emptyNote={emptyNote(source, query)}
            onOpen={(row) => {
              // A curated entry carries the numbers it is normally served at;
              // adopt them so the ladder's verdicts are taken at something
              // sensible rather than at whatever was last typed.
              const ctx = row.default_context ?? context
              const seqs = row.default_concurrency ?? concurrency
              if (row.default_context) setContext(ctx)
              if (row.default_concurrency) setConcurrency(seqs)
              openSheet({
                kind: 'model',
                id: row.model_id,
                context: ctx,
                concurrency: seqs,
              })
            }}
          />
        </div>
      </div>

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
