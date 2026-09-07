import { useRef, useState } from 'react'
import { SelectionProvider } from '../state/selection'
import { Header } from './Header'
import { Sheet } from './Sheet'
import { DashboardTab } from '../tabs/DashboardTab'
import { ModelsTab } from '../tabs/ModelsTab'
import { ClusterTab } from '../tabs/ClusterTab'
import { StorageTab } from '../tabs/StorageTab'
import { ChatTab } from '../tabs/ChatTab'
import { SpendTab } from '../tabs/SpendTab'
import { SettingsTab } from '../tabs/SettingsTab'
import { Sidebar } from '../sidebar/Sidebar'

export type Dest =
  | 'dash'
  | 'models'
  | 'cluster'
  | 'storage'
  | 'chat'
  | 'spend'
  | 'settings'

/** The chrome: header, the four destinations, the collapsible sidebar, and the
 *  one sheet. What each destination and each sidebar section actually shows is
 *  a later package's job (mockups-next: tabs/Dashboard, tabs/Cluster,
 *  tabs/Spend, tabs/Settings, sidebar/*) -- this file only builds the frame
 *  they land in and keeps it navigable on its own.
 *
 *  `SelectionProvider` lives here rather than in main.tsx: selection (which
 *  node, which link, which deployment, which sheet) is shell state, needed by
 *  `Sheet` below and by every destination and sidebar section a later package
 *  adds, and nothing outside the shell has a reason to reach it. */
/** Destinations that are themselves a split and want the full width.
 *
 *  Models puts a master list beside a detail pane; with the roster rail open as
 *  well that is three columns on a 1440px screen and the detail pane ends up
 *  narrower than the list feeding it. */
const WIDE: ReadonlySet<Dest> = new Set<Dest>(['models'])

export function AppShell() {
  const [dest, setDest] = useState<Dest>('dash')
  const [sidebarOpen, setSidebarOpen] = useState(true)
  // What the sidebar was before a wide destination collapsed it, so leaving
  // one restores the choice rather than silently reopening a rail somebody had
  // deliberately closed.
  const restore = useRef<boolean | null>(null)

  const go = (next: Dest) => {
    setDest(next)
    if (WIDE.has(next)) {
      if (restore.current === null) restore.current = sidebarOpen
      setSidebarOpen(false)
    } else if (restore.current !== null) {
      setSidebarOpen(restore.current)
      restore.current = null
    }
  }

  return (
    <SelectionProvider>
      <Header dest={dest} onSelectDest={go} />

      <div className={sidebarOpen ? 'wrap' : 'wrap narrow'}>
        <div className="rail">
          <button
            className="tab"
            aria-controls="side"
            aria-expanded={sidebarOpen}
            aria-label={sidebarOpen ? 'Hide sidebar' : 'Show sidebar'}
            title={sidebarOpen ? 'Hide sidebar' : 'Show sidebar'}
            onClick={() => {
              // An explicit toggle overrides the automatic collapse, including
              // on the way back out: once somebody has said what they want the
              // rail to do here, restoring an older value would fight them.
              restore.current = null
              setSidebarOpen((v) => !v)
            }}
          >
            <svg
              width="7"
              height="12"
              viewBox="0 0 7 12"
              fill="none"
              stroke="currentColor"
              strokeWidth={1.5}
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <path d="M1 1l5 5-5 5" />
            </svg>
          </button>
        </div>

        <main>
          <section role="tabpanel" hidden={dest !== 'dash'}>
            <DashboardTab />
          </section>
          <section role="tabpanel" hidden={dest !== 'models'}>
            <ModelsTab />
          </section>
          <section role="tabpanel" hidden={dest !== 'cluster'}>
            <ClusterTab />
          </section>
          <section role="tabpanel" hidden={dest !== 'storage'}>
            <StorageTab />
          </section>
          <section role="tabpanel" hidden={dest !== 'chat'}>
            <ChatTab />
          </section>
          <section role="tabpanel" hidden={dest !== 'spend'}>
            <SpendTab />
          </section>
          <section role="tabpanel" hidden={dest !== 'settings'}>
            <SettingsTab />
          </section>
        </main>

        <aside id="side">
          <div className="inner">
            <Sidebar />
          </div>
        </aside>
      </div>

      <Sheet />
    </SelectionProvider>
  )
}

