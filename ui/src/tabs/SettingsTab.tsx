import { useState } from 'react'
import { CoordinatorCard } from './settings/CoordinatorCard'
import { AddNodeCard } from './settings/AddNodeCard'
import { NodesCard } from './settings/NodesCard'
import { ProvidersCard } from './settings/ProvidersCard'
import { ClusterCard } from './settings/ClusterCard'
import { ContainmentCard } from './settings/ContainmentCard'
import { ReliabilityCard } from './settings/ReliabilityCard'
import { AppearanceCard } from './settings/AppearanceCard'
import { ScopeCards } from './settings/ScopeCards'
import { FilesystemsCard } from './storage/FilesystemsCard'
import { ModelCacheCard } from './storage/ModelCacheCard'
import { EstateCard } from './storage/EstateCard'
import { CollectionCard } from './storage/CollectionCard'

type Sub =
  | 'connection'
  | 'nodes'
  | 'storage'
  | 'providers'
  | 'policy'
  | 'appearance'
  | 'about'

const SUBS: { id: Sub; label: string }[] = [
  // Connection first for the same reason Coordinator used to be the first
  // card: it is the one section that still works when the connection does
  // not, and every other section is empty until the address in it is right.
  { id: 'connection', label: 'Connection' },
  { id: 'nodes', label: 'Nodes' },
  { id: 'storage', label: 'Storage' },
  { id: 'providers', label: 'Providers' },
  { id: 'policy', label: 'Policy' },
  { id: 'appearance', label: 'Appearance' },
  { id: 'about', label: 'About' },
]

/** The Settings destination: which coordinator this browser talks to, the
 *  machines and providers behind it, what they are allowed to cost, what they
 *  are spending their disk on, and what this project has said it will not
 *  build.
 *
 *  Seven sub-tabs (`.subs`, the same bar the Dashboard uses) rather than the
 *  one nine-card column this used to be. The column was not merely long: it
 *  ran four unrelated jobs together, and roughly half its height was the
 *  About cards, which are documentation and never change. Splitting on the
 *  job puts every screen inside one viewport.
 *
 *  Storage joined this tab rather than staying its own destination: it is the
 *  same job as Nodes -- reading facts off the machines behind this
 *  coordinator -- and the four cards (`tabs/storage/*`) moved in unchanged,
 *  including the docstring on why disk is read on demand and never sampled.
 *
 *  Every section stays mounted and is hidden with `hidden`, not unmounted.
 *  Add a node holds a minted enrollment token, its countdown, and the list of
 *  machines that have turned up since -- state a tab switch must not throw
 *  away -- and the same is true of a half-typed coordinator address.
 *
 *  Appearance joined the same way: the dark-mode select used to sit in the
 *  header, next to the destination tabs, which put a local-only preference
 *  in the one bar every screen shares. It reads and writes `theme.ts`, the
 *  same module the header now calls once on load to apply whatever was
 *  stored.
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
            id={`st-tab-${s.id}`}
            role="tab"
            type="button"
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
      <div
        id="st-connection"
        role="tabpanel"
        aria-labelledby="st-tab-connection"
        hidden={sub !== 'connection'}
      >
        <div className="settingsgrid">
          <CoordinatorCard />
          <ClusterCard />
        </div>
      </div>

      <div id="st-nodes" role="tabpanel" aria-labelledby="st-tab-nodes" hidden={sub !== 'nodes'}>
        <AddNodeCard />
        <NodesCard />
      </div>

      <div id="st-storage" role="tabpanel" aria-labelledby="st-tab-storage" hidden={sub !== 'storage'}>
        <FilesystemsCard />
        <ModelCacheCard />
        <EstateCard />
        <CollectionCard />
      </div>

      <div
        id="st-providers"
        role="tabpanel"
        aria-labelledby="st-tab-providers"
        hidden={sub !== 'providers'}
      >
        <ProvidersCard />
      </div>

      <div id="st-policy" role="tabpanel" aria-labelledby="st-tab-policy" hidden={sub !== 'policy'}>
        <ContainmentCard />
        <ReliabilityCard />
      </div>

      <div
        id="st-appearance"
        role="tabpanel"
        aria-labelledby="st-tab-appearance"
        hidden={sub !== 'appearance'}
      >
        <AppearanceCard />
      </div>

      {/* Not settings. Nothing here writes anything; they are the project's
          own record of what it has and has not built, which is why they sit
          behind their own tab instead of below the controls. */}
      <div
        id="st-about"
        role="tabpanel"
        aria-labelledby="st-tab-about"
        // Nothing in About is focusable, so the panel itself has to be, or a
        // keyboard lands on the tab with nowhere to go. Every other panel
        // holds a control and must not add a redundant stop.
        tabIndex={0}
        hidden={sub !== 'about'}
      >
        <ScopeCards />
      </div>
    </div>
  )
}
