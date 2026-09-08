import { useState } from 'react'
import { useStorage, useCluster } from '../../state/resources'
import { useBackend } from '../../state/backend'
import { CacheTable } from '../../components/CacheTable'
import type { CachedModel } from '../../api/types'
import { folderFor } from '../../api/modelcache'

const GiB = 1024 ** 3

function sizeLabel(b: number | null | undefined): string {
  if (b == null) return '—'
  if (b >= GiB) return `${(b / GiB).toFixed(1)} GB`
  if (b >= 1024 ** 2) return `${(b / 1024 ** 2).toFixed(0)} MB`
  return `${(b / 1024).toFixed(0)} KB`
}

/** Downloaded weights, per node, and the only way to get rid of them.
 *
 *  These are the biggest files on the machine by orders of magnitude — a
 *  single 120B repository is 182 GB — and nothing else in the product could
 *  see them. The control plane does not download them: the runtime container
 *  does, into the host cache it mounts, which is the same directory the node
 *  agent reads here. */
export function ModelCacheCard() {
  const storage = useStorage()
  const cluster = useCluster()
  const { backend, invalidate } = useBackend()
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [freed, setFreed] = useState<string | null>(null)

  const nodes = storage.data?.nodes ?? []

  // Anything a non-terminal deployment is serving. The button is withheld for
  // these, and the server refuses them regardless.
  const servedFolders = new Set(
    (cluster.data?.deployments ?? [])
      .filter((d) => d.state !== 'stopped' && d.state !== 'failed')
      .map((d) => folderFor(d.model_id ?? ''))
      .filter((f) => f !== 'models--'),
  )

  const remove = async (nodeId: string, m: CachedModel) => {
    if (
      !window.confirm(
        `Delete ${m.repo_id} from ${nodeId}?\n\n` +
          `This frees ${sizeLabel(m.bytes)} and cannot be undone. Serving this ` +
          `model again re-downloads it.`,
      )
    )
      return
    setBusy(`${nodeId}/${m.folder}`)
    setError(null)
    setFreed(null)
    try {
      const res = await backend.deleteCachedModel(nodeId, m.folder)
      setFreed(`${sizeLabel(res.bytes_freed)} freed from ${nodeId}`)
      invalidate()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(null)
    }
  }

  const anyCache = nodes.some((n) => n.models)

  return (
    <div className="card2">
      <h3>Downloaded models</h3>
      <div className="unit" style={{ marginBottom: 10 }}>
        Model weights on each node, largest first. These are pulled by the
        runtime, not by this control plane, into the cache it mounts — deleting
        one here frees the disk, and serving that model again downloads it
        afresh.
      </div>

      {storage.loading ? (
        <div className="unit">Measuring…</div>
      ) : !anyCache ? (
        <div className="unit">No node reported a model cache.</div>
      ) : (
        nodes.map((n) => (
          <CacheTable
            key={n.node_id}
            node={n}
            servedFolders={servedFolders}
            onDelete={remove}
            busy={busy}
          />
        ))
      )}

      {freed ? <div className="unit">{freed}</div> : null}
      {error ? (
        <div
          className="label"
          style={{ color: 'var(--fault)', marginTop: 8, whiteSpace: 'pre-wrap' }}
        >
          {error}
        </div>
      ) : null}
    </div>
  )
}
