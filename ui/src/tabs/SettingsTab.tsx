import { NodesCard } from './settings/NodesCard'
import { ProvidersCard } from './settings/ProvidersCard'
import { ClusterCard } from './settings/ClusterCard'
import { ScopeCards } from './settings/ScopeCards'

/** The Settings destination: nodes, providers, cluster identity, then scope
 *  and the two static "what this is not" cards. Ported from
 *  mockups-next/js/settings.js + derate.html's `#d-settings` section. */
export function SettingsTab() {
  return (
    <div>
      <NodesCard />
      <ProvidersCard />
      <ClusterCard />
      <ScopeCards />
    </div>
  )
}
