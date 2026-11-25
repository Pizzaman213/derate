import { useRouter } from '../state/router'
import { type UsageSub } from '../state/routes'
import { OverviewSub } from './spend/OverviewSub'
import { RequestsSub } from './requests/RequestsSub'
import { KeysSub } from './keys/KeysSub'
import { RateLimitsSub } from './requests/RateLimitsSub'
import { useCluster } from '../state/resources'
import { ResourceScope } from '../state/backend'

const SUBS: { id: UsageSub; label: string }[] = [
  { id: 'overview', label: 'Overview' },
  { id: 'requests', label: 'Requests' },
  { id: 'rate-limits', label: 'Rate limits' },
  { id: 'keys', label: 'Keys' },
]

/** The Usage destination: who used what, what it cost, and under which key.
 *
 *  These were three header entries -- Spend, Requests, Keys -- for one
 *  subject, and the code already treated them as one: the overview's per-key
 *  rows come from `keys/rows.ts`, a key's page embeds `requests/RequestRows`,
 *  every request row links to its key, and `?win=` was one route field with a
 *  rule per screen. One destination took the nav from eight entries to six.
 *
 *  Unlike Dashboard's and Settings' sub-tabs, the pane is in the URL
 *  (`/usage`, `/usage/requests`, `/usage/keys`), because a key's page and a
 *  request's page are linked from chat, setup, the health banner and the audit
 *  log -- `/usage/keys/<id>` has to open the Keys pane, not whichever one was
 *  showing. The panes stay mounted like every other `.subs` panel, so their
 *  polls, filters and drafts survive a switch. */
export function UsageTab() {
  const { route, navigate } = useRouter()
  const cluster = useCluster()
  const sub: UsageSub = route.sub ?? 'overview'
  // A pane is a screen, so picking one pushes. Clearing both subjects means
  // "Keys" from a key's page goes back to the list. `win` is left out on
  // purpose: it merges through, as the header did between the old /spend and
  // /requests, and href() drops `all` where the requests pane cannot draw it.
  const pick = (id: UsageSub) => {
    const model = route.fmodel
      ?? cluster.data?.deployments.find((d) => d.deployment_id === route.dep)?.served_name
      ?? route.dep
    navigate({
      dest: 'usage', sub: id, key: null, request: null,
      dep: id === 'rate-limits' ? route.dep : null,
      ...(id === 'requests' && sub === 'rate-limits'
        ? { fmodel: route.ftarget ? null : model, fstatus: '429' as const, fkey: null, fretried: false }
        : {}),
    })
  }

  return (
    <div>
      <div className="subs" role="tablist" aria-label="Usage sections">
        {SUBS.map((s) => (
          <button
            key={s.id}
            id={`us-tab-${s.id}`}
            role="tab"
            type="button"
            aria-selected={sub === s.id}
            aria-controls={`us-${s.id}`}
            onClick={() => pick(s.id)}
          >
            {s.label}
          </button>
        ))}
      </div>

      <div id="us-overview" role="tabpanel" aria-labelledby="us-tab-overview" hidden={sub !== 'overview'}>
        <ResourceScope active={sub === 'overview'}>
          <OverviewSub />
        </ResourceScope>
      </div>
      <div id="us-requests" role="tabpanel" aria-labelledby="us-tab-requests" hidden={sub !== 'requests'}>
        <ResourceScope active={sub === 'requests'}>
          <RequestsSub />
        </ResourceScope>
      </div>
      <div id="us-rate-limits" role="tabpanel" aria-labelledby="us-tab-rate-limits" hidden={sub !== 'rate-limits'}>
        <ResourceScope active={sub === 'rate-limits'}>
          {sub === 'rate-limits' ? <RateLimitsSub /> : null}
        </ResourceScope>
      </div>
      <div id="us-keys" role="tabpanel" aria-labelledby="us-tab-keys" hidden={sub !== 'keys'}>
        <ResourceScope active={sub === 'keys'}>
          <KeysSub />
        </ResourceScope>
      </div>
    </div>
  )
}
