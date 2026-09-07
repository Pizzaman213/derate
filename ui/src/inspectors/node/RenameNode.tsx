import { useRef, useState } from 'react'
import { useBackend } from '../../state/backend'
import { MAX_LABEL_LEN } from '../../state/names'

/** Give this machine a name.
 *
 *  The name is a caption and nothing else. `node_id` is the key every
 *  deployment, link measurement, routing target and saved graph arrangement is
 *  written against, and it does not move -- which is why the id stays on
 *  screen in the header above this field. A rename that hid the id would leave
 *  an operator unable to match the plate against the link chips, which is the
 *  confusion this whole feature exists to remove rather than relocate.
 *
 *  Why a name is needed at all: hostname is not unique. A worker in a
 *  `--network host` container reports the HOST's hostname, so a two-container
 *  Spark shows two machines calling themselves the same thing.
 *
 *  Empty saves as "no name", putting the node_id back. A control that could
 *  set a name but never clear one is a trap.
 */
export function RenameNode({ nodeId, label }: { nodeId: string; label: string | null }) {
  const { backend, invalidate } = useBackend()
  const [draft, setDraft] = useState(label ?? '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Which node and which server-side name the draft was seeded from. This page
  // stays mounted while the poll refreshes underneath it, so without this a
  // rename would either be reverted mid-edit by the next poll or, worse,
  // carried across to the next machine the operator opens.
  const seed = useRef({ nodeId, label })
  if (seed.current.nodeId !== nodeId || seed.current.label !== label) {
    seed.current = { nodeId, label }
    setDraft(label ?? '')
    setError(null)
  }

  const trimmed = draft.trim()
  const dirty = trimmed !== (label ?? '')

  // One path for both buttons. Clearing is a rename to nothing, and giving it
  // its own request would give it its own -- missing -- error handling.
  const commit = async (value: string) => {
    setBusy(true)
    setError(null)
    try {
      await backend.renameNode(nodeId, value)
      setDraft(value)
      invalidate()
    } catch (e) {
      // The gateway's own sentence: it is the thing that knows the length
      // limit and what was wrong with the name.
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div>
      <form
        style={{ display: 'flex', gap: 6, alignItems: 'center' }}
        onSubmit={(e) => {
          e.preventDefault()
          if (dirty && !busy) void commit(trimmed)
        }}
      >
        <input
          aria-label={`Name for ${nodeId}`}
          value={draft}
          maxLength={MAX_LABEL_LEN}
          placeholder={nodeId}
          disabled={busy}
          onChange={(e) => setDraft(e.target.value)}
          style={{ flex: 1, minWidth: 0 }}
        />
        <button type="submit" disabled={!dirty || busy}>
          {busy ? 'Saving…' : 'Rename'}
        </button>
        {label ? (
          <button type="button" disabled={busy} onClick={() => void commit('')}>
            Clear
          </button>
        ) : null}
      </form>
      <div className="unit" style={{ marginTop: 4 }}>
        {error ? (
          <span style={{ color: 'var(--fault)' }}>{error}</span>
        ) : (
          `Shown on the plate and everywhere this machine is named. ${nodeId} stays its id, so nothing that refers to it breaks.`
        )}
      </div>
    </div>
  )
}
