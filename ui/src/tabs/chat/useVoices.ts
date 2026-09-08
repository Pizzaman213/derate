import { useEffect, useState } from 'react'
import type { VoiceLibrary } from '../../api/types'
import { useBackend } from '../../state/backend'

/** `GET /v1/audio/voices` for one model, or nothing.
 *
 *  Fetched per model rather than polled: a voice library is a directory on the
 *  node the deployment runs on, and nothing changes it but somebody copying a
 *  file there. A 2 s poll would ask a question whose answer changes by hand,
 *  once, and the runtime only reads that directory at startup anyway.
 *
 *  `enabled` is what stops it being asked at all for a text model -- the
 *  gateway would answer `wrong_modality`, correctly, and the Chat tab would
 *  then have a refusal on screen about a control it is not showing.
 *
 *  A failure is returned, not thrown. Naming no voice is a real request and it
 *  works whatever this said, so the caller renders the sentence beside a form
 *  that still functions rather than in place of one. */
export function useVoices(
  model: string | null,
  enabled: boolean,
): { library: VoiceLibrary | null; error: string | null } {
  const { backend } = useBackend()
  const [library, setLibrary] = useState<VoiceLibrary | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (model === null || !enabled) {
      setLibrary(null)
      setError(null)
      return
    }
    // `cancelled` because a fast switch between two models must not let the
    // slower answer overwrite the newer one -- the classic out-of-order
    // resolve, and here it would offer one deployment's voices for another's.
    let cancelled = false
    setLibrary(null)
    setError(null)
    void backend
      .voices(model)
      .then((lib) => {
        if (!cancelled) setLibrary(lib)
      })
      .catch((e: unknown) => {
        // ApiError has already unwrapped the gateway's envelope, so this is
        // the server's own sentence.
        if (!cancelled) setError(e instanceof Error ? e.message : String(e))
      })
    return () => {
      cancelled = true
    }
  }, [model, enabled, backend])

  return { library, error }
}
