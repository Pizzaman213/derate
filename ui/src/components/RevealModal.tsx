import { useEffect, useRef } from 'react'
import { createPortal } from 'react-dom'
import { Copyable } from './Copyable'
import { containTab } from './focusTrap'

/** The one place a `drt_` API key is ever on screen.
 *
 *  The coordinator stores only a hash of the secret, so this is the reveal and
 *  there is no second one: the key's own page, the keys table and every other
 *  response carry the key id and never the secret (api/redact.ts scrubs the
 *  shape by value everywhere else). The caller holds the secret in state only
 *  until `onDone`, and must drop it then -- this component never re-renders
 *  it after "I saved it", because once it unmounts nothing holds it.
 *
 *  Escape and the scrim do NOT dismiss it. A reveal closed by a stray key
 *  press is a key nobody wrote down and has to be rotated; the button is the
 *  only way out, and it says what pressing it means. */
export function RevealModal({
  secret,
  keyId,
  onDone,
}: {
  secret: string
  keyId: string
  onDone(): void
}) {
  const cardRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const card = cardRef.current
    card?.focus()
    // Tab stays inside, like the Sheet. Escape is deliberately not handled:
    // see the comment above -- the button is the only way out.
    const onKeyDown = (e: KeyboardEvent) => containTab(e, card)
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [])

  return createPortal(
    <div className="sheet on" role="presentation">
      <div
        ref={cardRef}
        className="card reveal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="reveal-title"
        tabIndex={-1}
      >
        <h2 id="reveal-title">API key {keyId}</h2>
        <p>
          This is the only time the key is shown. derate keeps a hash of it, not the key, so it
          cannot be shown again -- if it is lost, rotate it.
        </p>
        <Copyable text={secret} label="Copy key" />
        <p className="unit">
          Send it as <span className="mono">Authorization: Bearer &lt;key&gt;</span> to{' '}
          <span className="mono">/v1</span>.
        </p>
        <button type="button" onClick={onDone}>
          I saved it
        </button>
      </div>
    </div>,
    document.body,
  )
}
