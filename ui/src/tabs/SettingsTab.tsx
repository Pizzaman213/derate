import { AddNodeCard } from './settings/AddNodeCard'
import { NodesCard } from './settings/NodesCard'
import { ProvidersCard } from './settings/ProvidersCard'
import { ClusterCard } from './settings/ClusterCard'
import { ScopeCards } from './settings/ScopeCards'

/** The Settings destination: how to add a node, the nodes themselves,
 *  providers, cluster identity, then scope and the two static "what this is
 *  not" cards. Ported from mockups-next/js/settings.js + derate.html's
 *  `#d-settings` section, plus AddNodeCard, which the mockup gestured at with
 *  an address form it could not wire up. */
export function SettingsTab() {
  return (
    <div>
      <AddNodeCard />
      <NodesCard />
      <ProvidersCard />
      <ClusterCard />
      <ScopeCards />
    </div>
  )
}
