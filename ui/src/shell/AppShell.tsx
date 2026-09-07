import { useEffect, useRef, useState } from 'react'
import { SelectionProvider } from '../state/selection'
import { useRouter, type Dest } from '../state/router'
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

// `Dest` is the router's -- the set of destinations and the set of path
// segments are the same set, and defining it twice is how they drift apart.
export type { Dest }

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
  const { route } = useRouter()
  const dest = route.dest
  const [sidebarOpen, setSidebarOpen] = useState(true)
  // What the sidebar was before a wide destination collapsed it, so leaving
  // one restores the choice rather than silently reopening a rail somebody had
  // deliberately closed.
  const restore = useRef<boolean | null>(null)
  // Mirrors the state so the effect below can read the current value without
  // taking it as a dependency -- it must run when the destination changes and
  // not when somebody toggles the rail.
  const openRef = useRef(true)
  const setSidebar = (open: boolean) => {
    openRef.current = open
    setSidebarOpen(open)
  }

  // Keyed on the destination rather than on the click that caused it: a
  // destination now also arrives from the Back button and from a pasted link,
  // and a rail that only collapsed when you clicked the tab yourself would
  // leave a shared /models link rendering its split in two thirds of the width.
  useEffect(() => {
    if (WIDE.has(dest)) {
      if (restore.current === null) restore.current = openRef.current
      setSidebar(false)
    } else if (restore.current !== null) {
      setSidebar(restore.current)
      restore.current = null
    }
  }, [dest])

  return (
    <SelectionProvider>
      <Header dest={dest} />

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
              setSidebar(!openRef.current)
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

        {/* Sections rather than tabpanels: the header's destinations are links
            to their own URLs now, not tabs, and a tabpanel with no tab pointing
            at it is a promise to a screen reader that nothing keeps. Each one
            still stays mounted while hidden, which is what keeps a destination's
            in-flight polls and scroll position across a visit elsewhere. */}
        <main>
          <section aria-label="Dashboard" hidden={dest !== 'dash'}>
            <DashboardTab />
          </section>
          <section aria-label="Models" hidden={dest !== 'models'}>
            <ModelsTab />
          </section>
          <section aria-label="Cluster" hidden={dest !== 'cluster'}>
            <ClusterTab />
          </section>
          <section aria-label="Storage" hidden={dest !== 'storage'}>
            <StorageTab />
          </section>
          <section aria-label="Chat" hidden={dest !== 'chat'}>
            <ChatTab />
          </section>
          <section aria-label="Spend" hidden={dest !== 'spend'}>
            <SpendTab />
          </section>
          <section aria-label="Settings" hidden={dest !== 'settings'}>
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

