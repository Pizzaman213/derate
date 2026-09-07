import { useState } from 'react'
import { CoordinatorCard } from './settings/CoordinatorCard'
import { AddNodeCard } from './settings/AddNodeCard'
import { NodesCard } from './settings/NodesCard'
import { ProvidersCard } from './settings/ProvidersCard'
import { ClusterCard } from './settings/ClusterCard'
import { ContainmentCard } from './settings/ContainmentCard'
import { ScopeCards } from './settings/ScopeCards'

type Sub = 'connection' | 'nodes' | 'providers' | 'policy' | 'about'

const SUBS: { id: Sub; label: string }[] = [
  // Connection first for the same reason Coordinator used to be the first
  // card: it is the one section that still works when the connection does
  // not, and every other section is empty until the address in it is right.
  { id: 'connection', label: 'Connection' },
  { id: 'nodes', label: 'Nodes' },
  { id: 'providers', label: 'Providers' },
  { id: 'policy', label: 'Policy' },
  { id: 'about', label: 'About' },
]

/** The Settings destination: which coordinator this browser talks to, the
 *  machines and providers behind it, what they are allowed to cost, and what
 *  this project has said it will not build.
 *
 *  Five sub-tabs (`.subs`, the same bar the Dashboard uses) rather than the
 *  one nine-card column this used to be. The column was not merely long: it
 *  ran four unrelated jobs together, and roughly half its height was the
 *  About cards, which are documentation and never change. Splitting on the
 *  job puts every screen inside one viewport.
 *
 *  Every section stays mounted and is hidden with `hidden`, not unmounted.
 *  Add a node holds a minted enrollment token, its countdown, and the list of
 *  machines that have turned up since -- state a tab switch must not throw
 *  away -- and the same is true of a half-typed coordinator address.
 *
 *  Ported from mockups-next/js/settings.js + derate.html's `#d-settings`
 *  section, plus AddNodeCard, which the mockup gestured at with an address
 *  form it could not wire up. */
export function SettingsTab() {
  const [sub, setSub] = useState<Sub>('connection')

  return (
    <div>
      <div className="subs" role="tablist" aria-label="Settings sections">
        {SUBS.map((s) => (
          <button
            key={s.id}
            role="tab"
            aria-selected={sub === s.id}
            aria-controls={`st-${s.id}`}
            onClick={() => setSub(s.id)}
          >
            {s.label}
          </button>
        ))}
      </div>

      {/* Where requests go, beside what answers them. Two cards, and the
          second is the proof the first is right: an address that resolves
          reports a cluster id back. */}
      <div id="st-connection" role="tabpanel" hidden={sub !== 'connection'}>
        <div className="settingsgrid">
          <CoordinatorCard />
          <ClusterCard />
        </div>
      </div>

      <div id="st-nodes" role="tabpanel" hidden={sub !== 'nodes'}>
        <AddNodeCard />
        <NodesCard />
      </div>

      <div id="st-providers" role="tabpanel" hidden={sub !== 'providers'}>
        <ProvidersCard />
      </div>

      <div id="st-policy" role="tabpanel" hidden={sub !== 'policy'}>
        <ContainmentCard />
      </div>

      {/* Not settings. Nothing here writes anything; they are the project's
          own record of what it has and has not built, which is why they sit
          behind their own tab instead of below the controls. */}
      <div id="st-about" role="tabpanel" hidden={sub !== 'about'}>
        <ScopeCards />
      </div>
    </div>
  )
}
