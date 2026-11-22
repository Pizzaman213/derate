import { useEffect, useRef } from 'react'
import { createPortal } from 'react-dom'
import { containTab, focusables } from '../components/focusTrap'
import { useSelection, type SheetTarget } from '../state/selection'
import { useCluster, useRouting, useSettings } from '../state/resources'
import { useMetrics } from '../state/metrics'
import type { Cluster, RoutingConfig, Settings } from '../api/types'
import type { SafeMetricsFrame } from '../state/useMetrics'
import { NodeInspector } from '../inspectors/node/NodeInspector'
import { DeploymentInspector } from '../inspectors/DeploymentInspector'
import { ModelPane } from '../tabs/models/ModelPane'
import { runners } from '../tabs/models/rows'

// The one modal host (mockups-next: a single `#sheet`/`#cardBody` pair reused
// by both the node inspector and the deployment inspector). Each inspector
// owns its own header row (lamp, label, Close) -- this file owns only the
// mechanics every use of the sheet needs regardless of which one is showing:
// the scrim, the scroll lock, and a focus trap that holds even across a
// content swap while the sheet stays open.

export function Sheet() {
  const { sheet, closeSheet } = useSelection()
  const cluster = useCluster()
  const routing = useRouting()
  const settings = useSettings()
  const { frame, stale } = useMetrics()

  const open = sheet !== null
  const cardRef = useRef<HTMLDivElement>(null)
  const restoreFocusRef = useRef<HTMLElement | null>(null)

  // Once per open session: remember what to hand focus back to, and lock the
  // page behind the modal. Reversed only when the sheet actually closes, not
  // when its content swaps underneath it (see the effect below) -- otherwise
  // switching from one node to another while the sheet stays open would
  // "remember" a button inside the sheet itself as the restore target.
  useEffect(() => {
    if (!open) return
    restoreFocusRef.current = document.activeElement as HTMLElement | null
    const prevOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.body.style.overflow = prevOverflow
      restoreFocusRef.current?.focus()
    }
  }, [open])

  // Initial focus, re-run whenever the sheet's kind/id changes while it stays
  // open (double-clicking a different node without closing the sheet first).
  // Without this, focus is left on whatever the PREVIOUS inspector rendered
  // at that DOM position -- a button the new inspector may not even have.
  useEffect(() => {
    if (!open) return
    ;(focusables(cardRef.current)[0] ?? cardRef.current)?.focus()
  }, [open, sheet?.kind, sheet?.id])

  useEffect(() => {
    if (!open) return

    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        closeSheet()
        return
      }
      containTab(e, cardRef.current)
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [open, closeSheet, sheet?.kind, sheet?.id])

  if (!sheet) return null

  // Three sizes. A model carries a quantization ladder, which needs the wide
  // card. A node carries its own telemetry over a window, what it is serving,
  // the requests that ran on it and its logs, which is a page rather than a
  // card -- and a deployment is now the same shape: two columns, the backend
  // log filling the right one, which a 700px card would have squeezed to two
  // narrow strips.
  //
  // Note that bare `.card` sets no max-height at all -- only `.wide` and
  // `.full` do. That is fine now every long inspector is one of the two, and
  // the only thing left on `.card` is `Gone`, which is two lines.
  const size =
    sheet.kind === 'node' || sheet.kind === 'dep'
      ? 'card full'
      : sheet.kind === 'model'
        ? 'card wide'
        : 'card'

  return createPortal(
    <div
      className="sheet on"
      // The scrim is not a control: Escape is the keyboard path, so the
      // click-to-dismiss is a pointer convenience and the element is
      // presentational to assistive technology.
      role="presentation"
      onClick={(e) => {
        // Scrim click closes; a click that started inside the card and
        // released here (a drag-selection) still targets the card, not this
        // element, so this only fires for an actual scrim click.
        if (e.target === e.currentTarget) closeSheet()
      }}
    >
      <div
        ref={cardRef}
        className={size}
        role="dialog"
        aria-modal="true"
        aria-label={
          sheet.kind === 'node'
            ? sheet.id
            : sheet.kind === 'model'
              ? `${sheet.id} quantizations`
              : `${sheet.id} deployment`
        }
        tabIndex={-1}
      >
        <SheetBody
          sheet={sheet}
          cluster={cluster.data}
          routing={routing.data}
          settings={settings.data}
          frame={frame}
          stale={stale}
          onClose={closeSheet}
        />
      </div>
    </div>,
    document.body,
  )
}

/** A plain function, not a component defined inside `Sheet` -- nesting it
 *  there would hand React a new function identity every render and remount
 *  the inspector (and drop focus) on every parent re-render, exactly the
 *  failure this file exists to prevent. */
function SheetBody({
  sheet,
  cluster,
  routing,
  settings,
  frame,
  stale,
  onClose,
}: {
  sheet: SheetTarget
  cluster: Cluster | null
  routing: RoutingConfig[] | null
  settings: Settings | null
  frame: SafeMetricsFrame | null
  stale: boolean
  onClose: () => void
}) {
  if (sheet.kind === 'model') {
    // Resolves itself: a model is not something the cluster roster holds, and
    // both of its payloads are hub-bound and belong outside `resources.ts`.
    return (
      <ModelPane
        key={sheet.id}
        modelId={sheet.id}
        context={sheet.context}
        concurrency={sheet.concurrency}
        onClose={onClose}
      />
    )
  }

  if (sheet.kind === 'node') {
    const node = cluster?.nodes.find((n) => n.profile.node_id === sheet.id)
    if (!node) return <Gone id={sheet.id} onClose={onClose} />
    return (
      <NodeInspector
        node={node}
        deployments={cluster?.deployments ?? []}
        routing={routing ?? []}
        coordinatorBuild={cluster?.summary.coordinator_build ?? null}
        agentFeatures={cluster?.summary.agent_features ?? []}
        frame={frame}
        stale={stale}
        onClose={onClose}
      />
    )
  }

  // A sheet is addressed by served name, and the ledger can hold several rows
  // under one: three failed attempts at a model that is now up are four rows
  // named the same thing, in store order. Taking the first match opened the
  // oldest corpse of a model that is serving right now. A live row wins; the
  // ledger row is the fallback, so a model whose every attempt failed still
  // opens and still says why.
  const named = cluster?.deployments.filter((d) => d.served_name === sheet.id) ?? []
  const dep = runners(named)[0] ?? named[0]
  if (!dep) return <Gone id={sheet.id} onClose={onClose} />
  const cfg = routing?.find((c) => c.served_name === sheet.id) ?? null
  return (
    <DeploymentInspector
      dep={dep}
      cfg={cfg}
      nodes={cluster?.nodes ?? []}
      frame={frame}
      stale={stale}
      settings={settings}
      onClose={onClose}
    />
  )
}

function Gone({ id, onClose }: { id: string; onClose: () => void }) {
  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
        <span className="label mono">{id}</span>
        <button type="button" onClick={onClose}>Close</button>
      </div>
      <p className="unit" style={{ marginTop: 'var(--space-8)' }}>
        No longer in the cluster.
      </p>
    </div>
  )
}
