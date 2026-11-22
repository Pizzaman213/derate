import { useState } from 'react'
import { Verbatim } from '../../components/Verbatim'
import { useBackend } from '../../state/backend'
import { servesOnCluster } from '../../state/runtime'
import { folderFor } from './folders'
import type { ModelSession } from './useModelSession'

/** The Serve footer's disk actions: fetch the selected variant's weights
 *  without launching, or remove them from every node that holds them. */
export function ServeActions({ s }: { s: ModelSession }) {
  const { backend, invalidate } = useBackend()
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const { variant, running } = s
  if (!variant || s.routing || !servesOnCluster(s.runtime)) return null

  const repo = variant.repo_id
  const holders = s.cache.nodes(repo)
  const incomplete = s.cache.complete(repo, variant.file_bytes) !== true
  const live = running != null || s.launching

  const download = () => {
    setBusy(true)
    setMessage(null)
    backend
      .startDownload({ model_id: repo, ...(variant.gguf_file ? { gguf_file: variant.gguf_file } : {}) })
      .then(() => {
        invalidate()
        setMessage('Downloading. Progress is in the Downloads panel.')
      })
      .catch((e: unknown) => setMessage(e instanceof Error ? e.message : String(e)))
      .finally(() => setBusy(false))
  }

  const remove = async () => {
    if (!window.confirm(`Delete ${repo} from ${holders.join(', ')}? It downloads again next time it is served.`)) return
    setBusy(true)
    setMessage(null)
    try {
      for (const node of holders) await backend.deleteCachedModel(node, folderFor(repo))
      invalidate()
    } catch (e) {
      // A 409 model_in_use names the deployment holding it.
      setMessage(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      {incomplete && !live ? (
        <button type="button" className="ghost" disabled={busy} onClick={download}>
          Download only
        </button>
      ) : null}
      {!live && holders.length ? (
        <button type="button" className="ghost" disabled={busy} onClick={() => void remove()}>
          Delete from disk
        </button>
      ) : null}
      {message ? <Verbatim text={message} size="unit" /> : null}
    </>
  )
}
