import { useCallback, useEffect, useMemo, useState } from 'react'
import type { RoutingPolicy } from './api/types'
import { SCENARIOS, getScenario, setScenario, type Scenario } from './api/fixtures'
import { useBackend } from './state/backend'
import {
  useCandidates,
  useCluster,
  useProviders,
  useRouting,
  useTopology,
} from './state/resources'
import { useMetrics } from './state/useMetrics'
import { Lamp } from './components/Lamp'
import { Section } from './components/Panel'
import { NodeRoster } from './panels/NodeRoster'
import { Discovery } from './panels/Discovery'
import { PlanPanel } from './panels/PlanPanel'
import { RoutingPanel } from './panels/RoutingPanel'
import { ProvidersPanel } from './panels/ProvidersPanel'
import { MainView } from './views/MainView'
import { ClusterGraph } from './views/ClusterGraph'
import { PlanView } from './views/PlanView'
import { NodeDetail } from './views/NodeDetail'

type View = 'instrument' | 'graph' | 'plan' | 'node'

const SIDEBAR = 312

export function App() {
  const { backend, invalidate } = useBackend()
  const [view, setView] = useState<View>('instrument')
  const [nodeId, setNodeId] = useState<string | null>(null)
  const [measuring, setMeasuring] = useState<string | null>(null)

  const cluster = useCluster()
  const topology = useTopology()
  const candidates = useCandidates()
  const routing = useRouting()
  const providers = useProviders()
  const metrics = useMetrics()

  useTheme()

  const openNode = useCallback((id: string) => {
    setNodeId(id)
    setView('node')
  }, [])

  const measure = useCallback(
    async (a: string, b: string) => {
      if (!backend) return
      setMeasuring([a, b].sort().join('~'))
      try {
        await backend.measureLink(a, b)
        invalidate()
      } finally {
        setMeasuring(null)
      }
    },
    [backend, invalidate],
  )

  const admit = useCallback(
    async (id: string) => {
      if (!backend) return
      await backend.admit(id)
      invalidate()
    },
    [backend, invalidate],
  )

  const setPolicy = useCallback(
    async (servedName: string, policy: RoutingPolicy) => {
      if (!backend) return
      await backend.setPolicy(servedName, policy)
      invalidate()
    },
    [backend, invalidate],
  )

  // Pairs the cluster can see but has never probed. The plan panel offers a
  // measurement for the first of them rather than starting one unasked.
  const unmeasuredPairs = useMemo<[string, string][]>(
    () =>
      (topology.data?.edges ?? [])
        .filter((e) => !e.measured)
        .map((e) => [e.src, e.dst] as [string, string]),
    [topology.data],
  )

  // The plan the instrument is currently about.
  const activePlan = useMemo(() => {
    const live = (cluster.data?.deployments ?? []).filter(
      (d) => d.state === 'ready' || d.state === 'degraded',
    )
    if (live.length === 0) return null
    return [...live].sort(
      (a, b) =>
        b.node_ids.length - a.node_ids.length ||
        a.deployment_id.localeCompare(b.deployment_id),
    )[0]!.plan
  }, [cluster.data])

  // Routing is only interesting once a served name has more than one target.
  const routingConfigs = (routing.data ?? []).filter((c) => c.targets.length > 1)

  const ready = cluster.data && topology.data

  return (
    <div style={{ minHeight: '100%', display: 'flex', flexDirection: 'column' }}>
      <Nameplate
        clusterId={cluster.data?.summary.cluster_id ?? null}
        coordinator={cluster.data?.summary.coordinator ?? null}
        stream={metrics.stream}
        mode={backend?.mode ?? null}
      />

      <div
        style={{
          flex: 1,
          display: 'grid',
          gridTemplateColumns: `minmax(0, 1fr) ${SIDEBAR}px`,
          alignItems: 'stretch',
        }}
      >
        <main style={{ padding: 'var(--s3)', minWidth: 0 }}>
          {!ready ? (
            <p className="label muted" style={{ fontWeight: 400 }}>
              {cluster.error ? cluster.error.message : 'Reading the cluster…'}
            </p>
          ) : view === 'graph' ? (
            <ClusterGraph
              topology={topology.data!}
              frame={metrics.frame}
              stale={metrics.stale}
              routing={routing.data ?? []}
              measuring={measuring}
              onSelectNode={openNode}
              onMeasure={(a, b) => void measure(a, b)}
            />
          ) : view === 'plan' ? (
            <PlanView onLaunched={() => setView('instrument')} />
          ) : view === 'node' && nodeId ? (
            <NodeDetail
              nodeId={nodeId}
              cluster={cluster.data!}
              topology={topology.data!}
              frame={metrics.frame}
              stale={metrics.stale}
              onBack={() => setView('instrument')}
              onMeasure={(a, b) => void measure(a, b)}
              measuring={measuring}
            />
          ) : (
            <MainView
              cluster={cluster.data!}
              topology={topology.data!}
              frame={metrics.frame}
              history={metrics.history}
              stale={metrics.stale}
              onPlanModel={() => setView('plan')}
            />
          )}
        </main>

        <aside
          style={{
            borderLeft: '1px solid var(--rule)',
            background: 'var(--panel-recessed)',
            minWidth: 0,
          }}
        >
          <nav style={{ padding: 'var(--s1)', display: 'grid', gap: 2 }}>
            <NavItem label="Instrument" on={view === 'instrument' || view === 'node'} onClick={() => setView('instrument')} />
            <NavItem label="Cluster graph" on={view === 'graph'} onClick={() => setView('graph')} />
            <NavItem label="Plan a model" on={view === 'plan'} onClick={() => setView('plan')} />
          </nav>
          <hr />

          <Section title="Nodes">
            {cluster.data ? (
              <NodeRoster
                nodes={cluster.data.nodes}
                frame={metrics.frame}
                streamStale={metrics.stale}
                onSelect={openNode}
                selected={view === 'node' ? nodeId : null}
              />
            ) : null}
          </Section>
          <hr />

          <Section title="Found on your network">
            <Discovery
              candidates={candidates.data ?? []}
              coordinatorAddress={
                cluster.data?.nodes.find(
                  (n) => n.profile.node_id === cluster.data?.summary.coordinator,
                )?.profile.address ?? null
              }
              onAdmit={admit}
            />
          </Section>
          <hr />

          <Section title="Plan">
            <PlanPanel
              plan={activePlan}
              unmeasuredPairs={unmeasuredPairs}
              measuring={measuring}
              onMeasure={(a, b) => void measure(a, b)}
            />
          </Section>

          {routingConfigs.length > 0 ? (
            <>
              <hr />
              <Section title="Routing">
                <RoutingPanel configs={routingConfigs} onPolicy={setPolicy} />
              </Section>
            </>
          ) : null}

          {(providers.data ?? []).length > 0 ? (
            <>
              <hr />
              <Section title="Providers">
                <ProvidersPanel providers={providers.data ?? []} />
              </Section>
            </>
          ) : null}
        </aside>
      </div>
    </div>
  )
}

function NavItem({
  label,
  on,
  onClick,
}: {
  label: string
  on: boolean
  onClick: () => void
}) {
  return (
    <button
      onClick={onClick}
      aria-current={on ? 'page' : undefined}
      style={{
        border: 0,
        borderRadius: 0,
        borderLeft: `2px solid ${on ? 'var(--ink)' : 'transparent'}`,
        padding: '4px 10px',
        textAlign: 'left',
        fontWeight: on ? 500 : 400,
        color: on ? 'var(--ink)' : 'var(--ink-muted)',
      }}
    >
      {label}
    </button>
  )
}

// ── Nameplate ────────────────────────────────────────────────────────────────

function Nameplate({
  clusterId,
  coordinator,
  stream,
  mode,
}: {
  clusterId: string | null
  coordinator: string | null
  stream: ReturnType<typeof useMetrics>['stream']
  mode: 'live' | 'fixture' | null
}) {
  const stale = stream.status === 'stale'
  return (
    <header
      style={{
        borderBottom: '1px solid var(--rule)',
        padding: '10px var(--s1)',
        display: 'flex',
        alignItems: 'center',
        gap: 'var(--s2)',
        flexWrap: 'wrap',
      }}
    >
      <span style={{ fontWeight: 500 }}>sparkplane</span>
      {clusterId ? (
        <span className="unit">
          {clusterId}
          {coordinator ? ` · coordinator ${coordinator}` : ''}
        </span>
      ) : null}

      <span style={{ flex: 1 }} />

      {/* Fixture data is labelled. Demo numbers that look live are worse than
          demo numbers that say what they are. */}
      {mode === 'fixture' ? <FixtureControls /> : null}

      <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <Lamp
          signal={stale ? 'fault' : stream.status === 'open' ? 'live' : 'idle'}
          hollow={stale}
          label={
            stale
              ? 'metrics stream disconnected, retrying'
              : stream.status === 'open'
                ? 'metrics stream connected'
                : 'connecting'
          }
        />
        <span className="unit">
          {stale
            ? `stream lost, retrying in ${Math.round(stream.retryInMs / 1000)} s`
            : stream.status === 'open'
              ? 'live'
              : 'connecting'}
        </span>
      </span>

      <ThemeToggle />
    </header>
  )
}

function FixtureControls() {
  const { invalidate } = useBackend()
  const [current, setCurrent] = useState<Scenario>(getScenario())
  return (
    <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
      <span className="unit">fixture data</span>
      <label>
        <span className="sr-only">Fixture scenario</span>
        <select
          value={current}
          onChange={(e) => {
            const s = e.target.value as Scenario
            setCurrent(s)
            setScenario(s)
            invalidate()
          }}
          style={{ fontSize: 12, padding: '2px 6px' }}
        >
          {SCENARIOS.map((s) => (
            <option key={s.id} value={s.id}>
              {s.label}
            </option>
          ))}
        </select>
      </label>
    </span>
  )
}

// ── Theme ────────────────────────────────────────────────────────────────────

type Theme = 'system' | 'light' | 'dark'

/** Dark mode is a token swap. Nothing here touches a component. */
function useTheme(): void {
  useEffect(() => {
    const stored = (localStorage.getItem('sparkplane.theme') as Theme) ?? 'system'
    apply(stored)
  }, [])
}

function apply(theme: Theme) {
  const root = document.documentElement
  if (theme === 'system') root.removeAttribute('data-theme')
  else root.setAttribute('data-theme', theme)
  localStorage.setItem('sparkplane.theme', theme)
}

function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(
    () => (localStorage.getItem('sparkplane.theme') as Theme) ?? 'system',
  )
  return (
    <label>
      <span className="sr-only">Colour scheme</span>
      <select
        value={theme}
        onChange={(e) => {
          const t = e.target.value as Theme
          setTheme(t)
          apply(t)
        }}
        style={{ fontSize: 12, padding: '2px 6px' }}
      >
        <option value="system">System</option>
        <option value="light">Light</option>
        <option value="dark">Dark</option>
      </select>
    </label>
  )
}
