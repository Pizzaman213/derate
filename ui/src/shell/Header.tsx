import { useEffect, useState } from 'react'
import { plainClick, useRouter, type Dest } from '../state/router'
import { useCluster, useSettings } from '../state/resources'
import { useMetrics } from '../state/metrics'
import { Lamp } from '../components/Lamp'

const DESTS: { id: Dest; label: string }[] = [
  { id: 'dash', label: 'Dashboard' },
  { id: 'models', label: 'Models' },
  { id: 'cluster', label: 'Cluster' },
  { id: 'storage', label: 'Storage' },
  { id: 'chat', label: 'Chat' },
  { id: 'spend', label: 'Spend' },
  { id: 'settings', label: 'Settings' },
]

interface Props {
  dest: Dest
}

/** The wordmark, the roster pill, the destination tabs, the theme control, and
 *  the one lamp that says whether the metrics stream is actually connected --
 *  everything mockups-next/derate.html puts in `<header>`. */
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

  return (
    <header>
      <svg width="32" height="24" viewBox="0 0 64 50" aria-label="derate">
        <path
          d="M26 25 V39 H56"
          fill="none"
          stroke="currentColor"
          strokeWidth={6}
          strokeLinecap="square"
          opacity={0.32}
        />
        <path
          d="M8 25 H26 V11 H56"
          fill="none"
          stroke="currentColor"
          strokeWidth={6}
          strokeLinecap="square"
        />
      </svg>
      <span style={{ fontWeight: 500, fontSize: 18, letterSpacing: '-.3px' }}>
        derate
      </span>

      {cluster.data ? (
        <span className="pill mono">
          {nodes.length} nodes · {nodes.filter((n) => n.healthy).length} healthy ·{' '}
          {serving} serving
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

// ── Theme ────────────────────────────────────────────────────────────────────
// Transplanted from the old App.tsx verbatim: dark mode is a token swap.

type Theme = 'system' | 'light' | 'dark'

/** Dark mode is a token swap. Nothing here touches a component. */
function useTheme(): void {
  useEffect(() => {
    const stored = (localStorage.getItem('derate.theme') as Theme) ?? 'system'
    apply(stored)
  }, [])
}

function apply(theme: Theme) {
  const root = document.documentElement
  if (theme === 'system') root.removeAttribute('data-theme')
  else root.setAttribute('data-theme', theme)
  localStorage.setItem('derate.theme', theme)
}

function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(
    () => (localStorage.getItem('derate.theme') as Theme) ?? 'system',
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
