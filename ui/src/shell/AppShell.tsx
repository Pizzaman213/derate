import { useState } from 'react'
import { SelectionProvider } from '../state/selection'
import { Header } from './Header'
import { Sheet } from './Sheet'

export type Dest = 'dash' | 'cluster' | 'spend' | 'settings'

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
export function AppShell() {
  const [dest, setDest] = useState<Dest>('dash')
  const [sidebarOpen, setSidebarOpen] = useState(true)

  return (
    <SelectionProvider>
      <Header dest={dest} onSelectDest={setDest} />

      <div className={sidebarOpen ? 'wrap' : 'wrap narrow'}>
        <div className="rail">
          <button
            className="tab"
            aria-controls="side"
            aria-expanded={sidebarOpen}
            aria-label={sidebarOpen ? 'Hide sidebar' : 'Show sidebar'}
            title={sidebarOpen ? 'Hide sidebar' : 'Show sidebar'}
            onClick={() => setSidebarOpen((v) => !v)}
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
            <Placeholder label="Dashboard" />
          </section>
          <section role="tabpanel" hidden={dest !== 'cluster'}>
            <Placeholder label="Cluster" />
          </section>
          <section role="tabpanel" hidden={dest !== 'spend'}>
            <Placeholder label="Spend" />
          </section>
          <section role="tabpanel" hidden={dest !== 'settings'}>
            <Placeholder label="Settings" />
          </section>
        </main>

        <aside id="side">
          <div className="inner">
            <SidebarSection title="Nodes" />
            <SidebarSection title="Plan" />
            <SidebarSection title="Routing" />
            <SidebarSection title="Cost per Mtok" />
          </div>
        </aside>
      </div>

      <Sheet />
    </SelectionProvider>
  )
}

function Placeholder({ label }: { label: string }) {
  return (
    <div className="stage">
      <p className="unit">{label}</p>
    </div>
  )
}

function SidebarSection({ title }: { title: string }) {
  return (
    <section>
      <h2>{title}</h2>
    </section>
  )
}
