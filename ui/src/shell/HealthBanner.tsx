import type { Route } from '../state/routes'
import { Banner } from '../components/Banner'
import { useKeyedResource } from '../state/backend'
import { useCluster, useProviders, useRouting } from '../state/resources'
import { plainClick, useRouter } from '../state/router'
import { fromState, nodeName } from '../state/names'
import { relativeTime } from '../format'

/** Every route field the Requests screen filters on, cleared, so the banner's
 *  link opens "failed over in the last day" and not that plus whatever key or
 *  model the current URL happened to carry. */
const RETRIED: Partial<Route> = {
  dest: 'usage',
  sub: 'requests',
  request: null,
  win: '24h',
  fkey: null,
  fmodel: null,
  fstatus: null,
  ftarget: null,
  fprovider: null,
  fretried: true,
}

/** The app-wide announcement that a machine is unreachable or a served name
 *  has lost targets, and how many requests have failed over since. Computed
 *  from what the API already carries; renders nothing when all is well, and
 *  leaves the moment the state clears -- there is nothing to dismiss. */
export function HealthBanner() {
  const { linkTo, navigate } = useRouter()
  const cluster = useCluster()
  const routing = useRouting()
  const providers = useProviders()

  const downNodes = (cluster.data?.nodes ?? []).filter((n) => !n.healthy)
  const configs = routing.data ?? []
  const lostTargets = configs.flatMap((c) =>
    c.targets
      .filter((t) => !t.healthy || (t.circuit != null && t.circuit !== 'closed'))
      .map((t) => ({ served: c.served_name, t })),
  )
  const since = downNodes.length ? Math.min(...downNodes.map((n) => n.last_seen)) : null

  const moved = useKeyedResource(
    String(since ?? ''),
    (b) => (since != null ? b.requests({ start: since, retried: true, page_size: 1 }) : Promise.resolve(null)),
    10_000,
  )

  if (downNodes.length === 0 && lostTargets.length === 0) return null

  // Why a target is down, in the words the API already has: the provider's own
  // last error for a remote target, else the breaker's state.
  const whyLost = (t: (typeof lostTargets)[number]['t']): string => {
    if (t.kind === 'remote') {
      const p = (providers.data ?? []).find((x) => t.target_id.startsWith(`${x.provider_id}:`))
      if (p?.last_error) return p.last_error
    }
    if (t.circuit != null && t.circuit !== 'closed') return `circuit ${t.circuit.replace('_', ' ')}`
    return 'unhealthy'
  }

  const sentences: string[] = downNodes.map(
    (n) => `${nodeName(fromState(n))} unreachable since ${relativeTime(n.last_seen)}`,
  )
  for (const c of configs) {
    const lost = lostTargets.filter((x) => x.served === c.served_name)
    if (lost.length === 0) continue
    const why = [...new Set(lost.map((x) => whyLost(x.t)))].join('; ')
    sentences.push(`${c.served_name}: ${lost.length} of ${c.targets.length} targets down (${why})`)
  }
  const total = moved.data?.total
  if (since != null && total != null) sentences.push(`${total} requests failed over since then`)

  // Warn only when nothing is actually down: every loss is a half-open breaker
  // (probing, not refusing) and no machine is unreachable.
  const onlyProbing =
    downNodes.length === 0 && lostTargets.every((x) => x.t.healthy && x.t.circuit === 'half_open')
  const href = linkTo(RETRIED)

  return (
    <Banner
      tone={onlyProbing ? 'warn' : 'fault'}
      action={{
        label: 'See requests',
        href,
        onClick: (e) => {
          if (!plainClick(e)) return
          e.preventDefault()
          navigate(RETRIED)
        },
      }}
    >
      {sentences.join(' · ')}
    </Banner>
  )
}
