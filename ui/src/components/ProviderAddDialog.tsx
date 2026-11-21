import { useEffect, useRef } from 'react'
import { createPortal } from 'react-dom'
import type { Provider, ProviderKindSpec } from '../api/types'
import { useProviderSecretRefs } from '../state/resources'
import { containTab } from './focusTrap'
import { Icon } from './Icon'
import { ProviderAddForm } from './ProviderAddForm'

/** Adding a provider without leaving the Models tab. The shell is
 *  `RevealModal`'s (portal, scrim, focus kept inside); the body is the same
 *  `ProviderAddForm` Settings renders. Unlike the reveal, nothing is lost by
 *  closing it, so Escape and Close both dismiss. */
export function ProviderAddDialog({
  providers,
  kinds,
  onClose,
}: {
  providers: Provider[]
  kinds: ProviderKindSpec[]
  onClose(): void
}) {
  const cardRef = useRef<HTMLDivElement>(null)
  // Mounted only while the dialog is open, so it costs nothing otherwise.
  const secretRefs = useProviderSecretRefs()

  useEffect(() => {
    const card = cardRef.current
    card?.focus()
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
      else containTab(e, card)
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [onClose])

  return createPortal(
    <div className="sheet on" role="presentation">
      <div
        ref={cardRef}
        className="card wide mdl-provider-add"
        role="dialog"
        aria-modal="true"
        aria-labelledby="provider-add-title"
        tabIndex={-1}
      >
        <div className="mdl-provider-add-head">
          <h2 id="provider-add-title">Add a provider</h2>
          <button type="button" className="ghost" aria-label="Close" onClick={onClose}>
            <Icon name="close" size={16} decorative />
          </button>
        </div>
        <p className="unit">
          A provider serves models over its own API. Once added, its models show in the Providers view.
        </p>
        <ProviderAddForm
          providers={providers}
          kinds={kinds}
          knownRefs={secretRefs.data ?? []}
          onAdded={onClose}
        />
      </div>
    </div>,
    document.body,
  )
}
