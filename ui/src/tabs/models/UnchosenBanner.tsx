// A provider that is serving its whole catalogue because nobody ever chose.
//
// Enrolling OpenRouter used to put every one of the several hundred models it
// publishes onto this cluster's /v1/models. The allowlist fixed that going
// forward -- a new provider serves nothing until somebody picks -- but a record
// written before it exists keeps serving everything, deliberately, because the
// alternative is an upgrade that silently stops routing.
//
// So the state is legitimate and it is also almost never what anybody wants.
// This says so where the models are, and offers the one click that ends it.

import { useState } from 'react'

import type { Provider } from '../../api/types'
import { useBackend } from '../../state/backend'

/** Providers serving a catalogue nobody picked from.
 *
 *  Two conditions, both required.
 *
 *  `models_chosen === false` and nothing weaker. `null` is "this port cannot
 *  say" -- the shipped stub does no accounting and answers null to every field
 *  in that group -- and the counts cannot stand in for it either: a provider
 *  whose operator switched everything on has `model_count === catalogue_count`
 *  exactly like a legacy one, and telling that person nothing was ever chosen
 *  would be false.
 *
 *  A published catalogue. A record saved before its first successful refresh
 *  has an empty model list, so there is nothing on /v1 to warn about -- and
 *  "Serve none" there would turn a passthrough into a permanent empty
 *  allowlist, pinning it to serving nothing for having been saved at the wrong
 *  moment. That is the trap the tri-state exists to avoid, so the button that
 *  would spring it is not offered. */
export function unchosenProviders(providers: Provider[] | null | undefined): Provider[] {
  return (providers ?? []).filter(
    (p) => p.models_chosen === false && (p.catalogue_count ?? 0) > 0,
  )
}

export function UnchosenBanner({ providers }: { providers: Provider[] | null | undefined }) {
  const { backend, invalidate } = useBackend()
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const unchosen = unchosenProviders(providers)
  if (!unchosen.length) return null

  const serveNone = async (p: Provider) => {
    setBusy(p.provider_id)
    setError(null)
    try {
      // The complete set, and it is empty. `[]` is a choice -- "serve nothing"
      // -- which is a different record from the absent value this banner is
      // about, and the server keeps them apart.
      await backend.patchProvider(p.provider_id, { enabled_models: [] })
      invalidate()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(null)
    }
  }

  return (
    <>
      {unchosen.map((p) => (
        <div
          key={p.provider_id}
          className="unit"
          style={{
            display: 'flex',
            gap: 'var(--s-2)',
            alignItems: 'baseline',
            flexWrap: 'wrap',
            color: 'var(--warn)',
            margin: '0 0 6px',
          }}
        >
          <span>
            {p.display_name || p.provider_id} is serving all{' '}
            {(p.catalogue_count ?? 0).toLocaleString()} models it publishes, because
            nothing was ever chosen. Every one of them is a name on this cluster’s
            API.
          </span>
          <button
            disabled={busy !== null}
            onClick={() => void serveNone(p)}
            title={`Stop serving every model ${p.display_name || p.provider_id} publishes`}
          >
            {busy === p.provider_id ? 'Switching off…' : 'Serve none'}
          </button>
        </div>
      ))}
      {error ? (
        <p
          className="label"
          style={{ fontWeight: 400, color: 'var(--warn)', whiteSpace: 'pre-wrap', margin: '0 0 6px' }}
        >
          {error}
        </p>
      ) : null}
    </>
  )
}
