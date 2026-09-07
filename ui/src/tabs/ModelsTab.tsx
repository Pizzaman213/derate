import { useEffect, useMemo, useRef, useState } from 'react'
import { useCatalog, useCluster, useProviders } from '../state/resources'
import { useBackend } from '../state/backend'
import type { ModelSearchResponse } from '../api/types'
import { useSelection } from '../state/selection'
import { QuantTableCard } from './models/QuantTableCard'
import { CatalogList, type CatalogEntry } from './models/CatalogList'

type Origin = 'catalog' | 'hub' | 'running' | 'providers'

const ORIGINS: { id: Origin; label: string }[] = [
  { id: 'catalog', label: 'Catalog' },
  { id: 'hub', label: 'Hub' },
  { id: 'running', label: 'Running' },
  { id: 'providers', label: 'Providers' },
]

/** Models: what you could run here, and what each quantization of it costs.
 *
 *  Opens on a populated list rather than an empty search box -- the curated
 *  shortlist comes from `GET /api/catalog`, which is the same list the capacity
 *  answer walks, so the picker and the verdict cannot drift apart.
 *
 *  Picking a model opens the one sheet, where the quantization ladder lives.
 *  That costs the ability to compare a variant against the list behind it,
 *  which is the accepted trade for not inventing a second modal host. */
export function ModelsTab() {
  const catalog = useCatalog()
  const cluster = useCluster()
  const providers = useProviders()
  const { openSheet } = useSelection()

  const [origin, setOrigin] = useState<Origin>('catalog')
  const [query, setQuery] = useState('')
  const [context, setContext] = useState(8192)
  const [concurrency, setConcurrency] = useState(4)

  const { backend } = useBackend()
  const [hits, setHits] = useState<ModelSearchResponse | null>(null)
  const [searching, setSearching] = useState(false)
  const [searchError, setSearchError] = useState<string | null>(null)
  // One sequence for the whole tab, bumped on every change including the
  // early return -- an answer for a query the field no longer holds must not
  // land. Same pattern, and same reason, as PlannerBar.
  const seq = useRef(0)

  useEffect(() => {
    if (origin !== 'hub') return
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
  }, [backend, origin, query])

  const entries = useMemo<CatalogEntry[]>(() => {
    if (origin === 'hub') {
      return (hits?.results ?? []).map((h) => ({
        model_id: h.model_id,
        label: h.model_id,
        detail: [
          h.quant_hint ?? undefined,
          h.downloads != null ? `${h.downloads.toLocaleString()} downloads` : undefined,
          h.pipeline_tag ?? undefined,
        ]
          .filter(Boolean)
          .join(' \u00b7 '),
        group: h.model_id.split('/')[0] ?? 'HuggingFace',
      }))
    }
    if (origin === 'running') {
      return (cluster.data?.deployments ?? []).map((d) => ({
        model_id: d.model_id,
        label: d.served_name,
        detail: `${d.runtime} · ${d.state}`,
        group: 'Running here',
      }))
    }
    if (origin === 'providers') {
      const out: CatalogEntry[] = []
      for (const p of providers.data ?? []) {
        for (const m of p.models ?? []) {
          out.push({
            model_id: m.upstream_id,
            label: m.served_name,
            detail: m.context_length ? `${m.context_length.toLocaleString()} ctx` : '',
            group: p.display_name || p.provider_id,
            // A remote model is served by somebody else's hardware; there is
            // nothing here to resolve it against.
            remote: true,
          })
        }
      }
      return out
    }
    return (catalog.data ?? []).map((m) => ({
      model_id: m.model_id,
      label: m.label,
      detail: m.detail,
      group: family(m.model_id),
      default_context: m.default_context,
      default_concurrency: m.default_concurrency,
    }))
  }, [origin, catalog.data, cluster.data, providers.data, hits])

  const filtered = useMemo(() => {
    // The hub search already applied the query; filtering again would drop
    // rows the hub matched on a field the label does not show.
    if (origin === 'hub') return entries
    const needle = query.trim().toLowerCase()
    if (!needle) return entries
    return entries.filter(
      (e) =>
        e.model_id.toLowerCase().includes(needle) ||
        e.label.toLowerCase().includes(needle),
    )
  }, [entries, query, origin])

  const source =
    origin === 'catalog'
      ? catalog
      : origin === 'hub'
        ? { loading: searching, error: searchError ? new Error(searchError) : null }
        : origin === 'running'
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
          {ORIGINS.map((o) => (
            <button
              key={o.id}
              role="tab"
              aria-pressed={origin === o.id}
              aria-selected={origin === o.id}
              onClick={() => setOrigin(o.id)}
            >
              {o.label}
            </button>
          ))}
        </div>

        <div className="bararea">
          <div className="fld" style={{ flex: 1, minWidth: 200 }}>
            <label htmlFor="mt-q">Search</label>
            <input
              id="mt-q"
              value={query}
              placeholder={origin === 'hub' ? 'search HuggingFace' : 'name or id'}
              spellCheck={false}
              onChange={(e) => setQuery(e.target.value)}
            />
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

        {origin === 'hub' && hits ? (
          <>
            {/* The hub failing greys one source; it does not empty the screen,
                and it says why in the resolver's own words. */}
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
          entries={filtered}
          loading={source.loading}
          error={source.error}
          emptyNote={
            origin === 'hub' && !query.trim()
              ? 'Type to search HuggingFace.'
              : query.trim()
              ? `Nothing here matches ${query.trim()}.`
              : origin === 'running'
                ? 'Nothing is deployed yet.'
                : origin === 'providers'
                  ? 'No provider has published a model list.'
                  : 'The catalog is empty.'
          }
          onOpen={(entry) => {
            // A curated entry carries the numbers it is normally served at;
            // adopt them so the ladder's verdicts are taken at something
            // sensible rather than at whatever was last typed.
            const ctx = entry.default_context ?? context
            const seqs = entry.default_concurrency ?? concurrency
            if (entry.default_context) setContext(ctx)
            if (entry.default_concurrency) setConcurrency(seqs)
            openSheet({
              kind: 'model',
              id: entry.model_id,
              context: ctx,
              concurrency: seqs,
            })
          }}
        />
      </div>

      <QuantTableCard />
    </div>
  )
}

/** Group by publisher-ish family so the list reads as sections rather than a
 *  flat wall. Purely presentational -- nothing downstream depends on it. */
function family(modelId: string): string {
  const name = modelId.split('/').pop() ?? modelId
  const first = name.split(/[-_.]/)[0] ?? name
  return first.charAt(0).toUpperCase() + first.slice(1)
}
