import { useCallback, useLayoutEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import type { LaunchRequest, Profile } from '../api/types'
import { useRouter } from '../state/router'
import { useBackend } from '../state/backend'
import { useCluster, useMemoryReport } from '../state/resources'
import { useMetrics } from '../state/metrics'
import type { CustomServe } from '../state/customServes'
import { band, TERMINAL, type ModelRow, type Sort } from './models/rows'
import { isListView, useModelList, type ListView } from './models/useModelList'
import { parseHfLink } from './models/query'
import { ModelToolbar } from './models/ModelToolbar'
import { DownloadsPanel, useDownloadsBadge } from './models/DownloadsPanel'
import { ModelRows, ListCaption, emptyNote } from './models/ModelRows'
import { HubCards, hubFilters, type HubSource } from './models/HubCards'
import { ProfilesList } from './models/ProfilesList'
import { ProviderAddDialog } from '../components/ProviderAddDialog'
import { WorthTrying } from './models/WorthTrying'
import { ModelPane } from './models/ModelPane'
import { SplitHandle, useSideWidth } from './models/SplitHandle'
import { UnchosenBanner } from './models/UnchosenBanner'
import { CustomServesCard } from './models/CustomServes'

const VIEW_KEY = 'derate.models.view'

/** Models: what you could run here, as a list beside one model's detail.
 *
 *  The list is ONE merged list (`useModelList`) seen through four views; the
 *  detail is one model's session (`ModelPane`). They share the URL and nothing
 *  else: the model is the path (`/models/<id>`), the numbers a verdict is
 *  taken at are `?ctx=`/`?seq=`, and the machines are `?on=` -- so a link is
 *  the whole question, and the list's bands and the pane's verdict are always
 *  answering it at the same numbers. */
export function ModelsTab() {
  const { route, navigate } = useRouter()
  const cluster = useCluster()
  const memory = useMemoryReport()
  // One metrics subscription for the whole list; running rows read their
  // engine's frame out of this map rather than subscribing per row.
  const { frame } = useMetrics()
  const live = useMemo(
    () => new Map((frame?.deployments ?? []).map((m) => [m.deployment_id, m])),
    [frame],
  )

  const [query, setQuery] = useState('')
  // Which view, remembered: what somebody comes here to do does not change
  // between visits. The old `cards`/`rows` values fail validation and land on
  // the default.
  const [view, setViewState] = useState<ListView>(() => {
    const stored = window.localStorage.getItem(VIEW_KEY)
    return isListView(stored) ? stored : 'fits'
  })
  const setView = (v: ListView) => {
    setViewState(v)
    window.localStorage.setItem(VIEW_KEY, v)
  }
  const [sort, setSort] = useState<Sort>('fit')
  const [showWont, setShowWont] = useState(false)
  const [showDownloads, setShowDownloads] = useState(false)
  const [onlyFits, setOnlyFits] = useState(false)
  const [hubSource, setHubSource] = useState<HubSource>('original')
  const [hubKind, setHubKind] = useState('all')
  const [addingProvider, setAddingProvider] = useState(false)
  const closeAddProvider = useCallback(() => setAddingProvider(false), [])
  // A saved setup the Profiles view's Load handed over, applied by the pane's
  // session once its ladder lands. `seq` remounts the pane so loading a
  // profile for the model already open applies it too.
  const [pendingRequest, setPendingRequest] = useState<{
    request: LaunchRequest
    profile: Profile | null
    seq: number
  } | null>(null)
  const { backend, invalidate } = useBackend()
  const [pinError, setPinError] = useState<string | null>(null)
  // Seeds the custom-command field for the model the custom-serves box just
  // opened. Component state, not URL: a one-time choice made on the way in.
  const [pendingCustomCommand, setPendingCustomCommand] = useState<string | undefined>(undefined)
  // The quantization a pasted link named, preferred when the ladder lands.
  const [pendingQuant, setPendingQuant] = useState<string | null>(null)
  const [linkNote, setLinkNote] = useState('')

  const list = useModelList(query, sort, view, showWont, hubFilters(hubSource, hubKind))

  const togglePin = (modelId: string) => {
    const current = [...list.pins]
    const next = list.pins.has(modelId)
      ? current.filter((id) => id !== modelId)
      : [...current, modelId]
    setPinError(null)
    backend
      .setPins(next)
      .then(() => invalidate())
      .catch((e: unknown) => setPinError(e instanceof Error ? e.message : String(e)))
  }

  const loadProfile = (p: Profile) => {
    setPendingCustomCommand(undefined)
    setPendingQuant(null)
    setPendingRequest((prev) => ({ request: p.request, profile: p, seq: (prev?.seq ?? 0) + 1 }))
    navigate({ dest: 'models', model: p.model_id })
  }
  const badge = useDownloadsBadge()
  const [side, setSide] = useSideWidth()
  const splitRef = useRef<HTMLDivElement>(null)
  // The page's height outside the panes -- everything above the split plus
  // <main>'s own gutter below it -- so the panes run to the bottom of the
  // window instead of stopping at a fixed share of it. Re-measured when
  // anything above changes height (a link note, the downloads panel).
  const [chrome, setChrome] = useState(0)
  useLayoutEffect(() => {
    const el = splitRef.current
    if (!el) return
    const measure = () => {
      const main = el.closest('main')
      // main's own bottom padding, read from its style: its box has a
      // min-height, so measuring its bottom edge would feed the panes'
      // height back into this number.
      const below = main ? parseFloat(getComputedStyle(main).paddingBottom) || 0 : 0
      setChrome(Math.ceil(el.getBoundingClientRect().top + window.scrollY + below))
    }
    measure()
    // <main>, not <body>: the body is pinned to the window's height, so it
    // never resizes when the toolbar above the split grows.
    const ro = new ResizeObserver(measure)
    ro.observe(el.closest('main') ?? document.body)
    window.addEventListener('resize', measure)
    return () => {
      ro.disconnect()
      window.removeEventListener('resize', measure)
    }
  }, [])

  const selected = route.model

  /** Adopt the numbers a curated entry is normally served at, so the verdict
   *  is taken at something sensible. One navigation, not three: the model and
   *  the numbers it is judged at are one state. */
  const open = (row: ModelRow) => {
    // A normal open is never a replay of a custom serve or a pasted link.
    setPendingCustomCommand(undefined)
    setPendingQuant(null)
    setPendingRequest(null)
    navigate({
      dest: 'models',
      model: row.model_id,
      ...(row.default_context ? { context: row.default_context } : {}),
      ...(row.default_concurrency ? { concurrency: row.default_concurrency } : {}),
    })
  }

  // From a suggestion or a pasted link. A link names a repository that may be
  // known nowhere else, so the pane opens on it cold and resolves it.
  const onOpen = (modelId: string, quant: string | null, fromLink: boolean) => {
    setPendingCustomCommand(undefined)
    setPendingQuant(quant)
    setPendingRequest(null)
    navigate({ dest: 'models', model: modelId })
    if (fromLink) {
      const link = parseHfLink(query)
      const repo = link?.repo ?? modelId
      const known = list.rows.some((r) => r.model_id === repo)
      setLinkNote(
        known
          ? `Opened ${repo}${quant ? ` at ${quant}` : ''} from the link.`
          : `Added ${repo} from the link. derate reads its config next, then checks the fit.`,
      )
      setQuery('')
    }
  }

  // Reopens the model a custom serve was launched on, with its command ready
  // to replay. The coordinator picks the numbers, as for a model opened cold.
  const openCustomServe = (serve: CustomServe) => {
    setPendingCustomCommand(serve.command)
    setPendingQuant(null)
    setPendingRequest(null)
    navigate({ dest: 'models', model: serve.modelId })
  }

  const visible = list.groups.flatMap((g) => g.rows)
  const hubRows = onlyFits
    ? visible.filter((r) => {
        const b = band(r)
        return b === 'running' || b === 'fits' || b === 'degraded'
      })
    : visible
  const shownCount = view === 'hub' ? hubRows.length : visible.length + (list.hubOnlyGroup?.rows.length ?? 0)
  const suggestNodes = useMemo(() => {
    const free = new Map((memory.data?.nodes ?? []).map((m) => [m.node_id, m.allocatable ?? null]))
    return (cluster.data?.nodes ?? []).map((n) => {
      const id = n.profile.node_id
      const running = list.rows.filter((r) =>
        r.deployments.some((d) => !TERMINAL.has(d.state) && d.node_ids.includes(id)),
      ).length
      return { id, freeBytes: free.get(id) ?? null, running }
    })
  }, [cluster.data, memory.data, list.rows])

  return (
    <div className="mdl-tab">
      <ModelToolbar
        query={query}
        onQuery={setQuery}
        view={view}
        onView={setView}
        counts={list.counts}
        sort={sort}
        onSort={setSort}
        nodes={suggestNodes}
        rows={list.rows}
        filterTokens={list.parsed.filterTokens}
        shownCount={shownCount}
        showDownloads={showDownloads}
        onShowDownloads={setShowDownloads}
        downloadsBadge={badge}
        onOpen={onOpen}
        linkNote={linkNote}
        onDismissLinkNote={() => setLinkNote('')}
        pins={list.pins}
        onAddProvider={() => setAddingProvider(true)}
      />

      {addingProvider ? (
        <ProviderAddDialog
          providers={list.providers.data ?? []}
          kinds={list.providerKinds.data ?? []}
          onClose={closeAddProvider}
        />
      ) : null}

      {showDownloads ? <DownloadsPanel providerKinds={list.providerKinds.data} /> : null}

      <div
        ref={splitRef}
        // Nothing open: the list takes the whole width. The placeholder pane
        // beside it said only "pick a model" and cost half the page.
        className={selected ? 'mdl-split' : 'mdl-split mdl-split-solo'}
        style={{ '--mdl-side': `${side * 100}%`, '--mdl-chrome': `${chrome}px` } as CSSProperties}
      >
        <div className="card2 mdl-pane" id="mt-panel">
          {/* A provider serving its whole catalogue because nobody ever chose.
              A statement about which rows are on the API, so it belongs to
              the view that is about providers. */}
          {view === 'providers' ? <UnchosenBanner providers={list.providers.data} /> : null}

          <CustomServesCard onReuse={openCustomServe} />

          {pinError ? <p className="label mdl-fatal">{pinError}</p> : null}

          {view === 'profiles' ? (
            <ProfilesList profiles={list.profiles} words={list.parsed.words} onLoad={loadProfile} />
          ) : view === 'hub' ? (
            <HubCards
              rows={hubRows}
              query={list.parsed.words}
              selectedId={selected}
              onOpen={open}
              loading={list.searching || list.loading}
              onlyFits={onlyFits}
              onOnlyFits={setOnlyFits}
              source={hubSource}
              onSource={setHubSource}
              kind={hubKind}
              onKind={setHubKind}
            />
          ) : (
            <>
              <ModelRows
                live={live}
                groups={list.groups}
                pinnedGroup={list.pinnedGroup}
                hubOnlyGroup={list.hubOnlyGroup}
                pins={list.pins}
                onTogglePin={togglePin}
                view={view}
                words={list.parsed.words}
                selectedId={selected}
                onOpen={open}
                loading={list.loading}
                fatal={list.fatal}
                emptyNote={emptyNote(query)}
                wontCount={list.wontCount}
                showWont={showWont}
                onShowWont={setShowWont}
                caption={
                  <ListCaption
                    capacity={list.capacity}
                    cap={list.cap}
                    problems={list.problems}
                    hits={list.hits}
                  />
                }
              />
              {view === 'fits' && !query.trim() ? (
                <WorthTrying
                  rows={list.worthTrying}
                  onOpen={open}
                  onBrowse={() => {
                    setView('hub')
                    setOnlyFits(true)
                  }}
                />
              ) : null}
            </>
          )}
        </div>

        {selected ? (
          <>
            <SplitHandle width={side} onWidth={setSide} container={splitRef} />

            {/* Keyed on the id so switching models remounts rather than
                letting the previous model's session linger under the new
                heading while its own requests are still in flight. The
                debounced numbers, not the raw URL: the same pair the capacity
                walk is asked for, so the list's bands and this pane's verdict
                answer one question. */}
            <div className="card2 mdl-pane">
              <ModelPane
                key={`${selected}#${pendingRequest?.seq ?? 0}`}
                modelId={selected}
                context={list.capContext}
                concurrency={list.capConcurrency}
                providers={list.providers.data}
                providerKinds={list.providerKinds.data}
                catalogues={list.catalogues.data?.byProvider ?? null}
                catalogueErrors={list.catalogues.data?.failed ?? null}
                // Open on the routing side for a row that exists only because a
                // provider publishes it: there is nothing here to run.
                preferRoute={list.rows.find((r) => r.model_id === selected)?.unservedOnly === true}
                initialCustomCommand={pendingCustomCommand}
                preferQuant={pendingQuant}
                initialRequest={
                  pendingRequest && pendingRequest.profile?.model_id === selected
                    ? { request: pendingRequest.request, profile: pendingRequest.profile }
                    : undefined
                }
                row={list.rows.find((r) => r.model_id === selected) ?? null}
                // Replace, so Back leaves the tab rather than reopening what you
                // just closed.
                onClose={() => navigate({ model: null }, { replace: true })}
              />
            </div>
          </>
        ) : null}
      </div>
    </div>
  )
}
