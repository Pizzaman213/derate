import { useEffect } from 'react'
import { Icon } from '../components/Icon'
import { plainClick, useRouter, type Dest } from '../state/router'
import { useCluster, useRouting, useSettings } from '../state/resources'
import { useMetrics } from '../state/metrics'
import { applyTheme, loadTheme } from '../theme'

const DESTS: { id: Dest; label: string }[] = [
  { id: 'dash', label: 'Dashboard' },
  { id: 'models', label: 'Models' },
  { id: 'cluster', label: 'Cluster' },
  { id: 'chat', label: 'Chat' },
  { id: 'usage', label: 'Usage' },
  { id: 'settings', label: 'Settings' },
]

interface Props {
  dest: Dest
}

/** The wordmark, the roster pill, and the destination tabs -- everything
 *  mockups-next/derate.html puts in `<header>`. A dropped metrics stream
 *  turns the bar's own rule line red (see .stream-fault in derate.css)
 *  instead of a separate lamp. The theme control that used to live here
 *  moved to Settings -> Appearance; this still applies the stored theme on
 *  load so it takes effect on every destination, not only that one. */
export function Header({ dest }: Props) {
  useTheme()
  const { linkTo, navigate } = useRouter()
  const cluster = useCluster()
  const settings = useSettings()
  const { stream } = useMetrics()
  const stale = stream.status === 'stale'

  const nodes = cluster.data?.nodes ?? []
  const deployments = cluster.data?.deployments ?? []
  const serving = deployments.filter(
    (d) => d.state === 'ready' || d.state === 'degraded',
  ).length
  // Served names with a healthy target, provider-served ones included; the
  // deployments count stands in only until routing has answered.
  const routing = useRouting()
  const servingNames = routing.data
    ? routing.data.filter((c) => c.targets.some((t) => t.healthy)).length
    : null

  return (
    <header className={stale ? 'stream-fault' : undefined}>
      {/* The monogram's solid stroke sits above the icon's own bounding-box
          centre -- the faint echo stroke below it doesn't carry the same
          visual weight -- so flex centring against the wordmark leaves the
          mark looking high. `.brand-mark` nudges it down to match and sizes
          the 24-box so the ink is the 27px it always was; SetupTab's copy
          gets the same treatment in setup.css. */}
      <Icon name="derate-mark" label="derate" size={24} className="brand-mark" />
      <span style={{ fontWeight: 500, fontSize: 'var(--size-wordmark)', letterSpacing: 'var(--tracking-tight)' }}>
        derate
      </span>

      {cluster.data ? (
        <span className="pill mono">
          {nodes.length} nodes · {nodes.filter((n) => n.healthy).length} healthy ·{' '}
          {servingNames ?? serving} serving
        </span>
      ) : null}
      {settings.data?.local_only ? <span className="pill mono">cloud off</span> : null}

      {/* Anchors, not buttons. Each destination has a real URL now, and a real
          URL is only worth having if the browser's own affordances reach it:
          right-click to copy the link to the Cluster view, middle-click to open
          Models in a second tab, hover to see where a tab goes. The click
          handler takes plain clicks so the app navigates without a reload; a
          modified click falls through to the browser, which is the whole
          point. */}
      <nav className="dest" aria-label="Destination">
        {DESTS.map((d) => (
          <a
            key={d.id}
            href={linkTo({ dest: d.id })}
            aria-current={dest === d.id ? 'page' : undefined}
            onClick={(e) => {
              if (!plainClick(e)) return
              e.preventDefault()
              navigate({ dest: d.id })
            }}
          >
            {d.label}
          </a>
        ))}
      </nav>
    </header>
  )
}

/** Applies the stored theme once on mount, so it takes effect app-wide
 *  regardless of which destination renders first. The control that changes
 *  it lives in Settings -> Appearance (tabs/settings/AppearanceCard.tsx). */
function useTheme(): void {
  useEffect(() => {
    applyTheme(loadTheme())
  }, [])
}
