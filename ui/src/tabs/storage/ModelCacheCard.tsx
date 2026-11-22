import { useState } from 'react'
import { useBackend } from '../../state/backend'
import type { Resource } from '../../state/backend'
import { CacheTable } from '../../components/CacheTable'
import type { CachedModel, Cluster, ReplicationState, StorageReport } from '../../api/types'
import { copyBlocked } from './presence'
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
export function ModelCacheCard({
  storage,
  cluster,
  replication,
}: {
  storage: Resource<StorageReport | null>
  cluster: Resource<Cluster>
  replication: Resource<ReplicationState>
}) {
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

  // Copies go out of the coordinator's cache only, so only its table offers
  // them, one button per other node.
  const coordinatorId = cluster.data?.nodes.find((n) => n.role === 'coordinator')?.profile.node_id
  const targets = (cluster.data?.nodes ?? [])
    .map((n) => n.profile.node_id)
    .filter((id) => id !== coordinatorId)
  // Read off the same fan-out, so a target whose agent cannot take a copy is
  // a disabled button with its reason rather than a failure on click.
  const blocked = new Map<string, string>()
  for (const t of targets) {
    const why = copyBlocked(nodes, t)
    if (why) blocked.set(t, why)
  }

  const copy = async (m: CachedModel, target: string) => {
    setBusy(`copy:${target}/${m.folder}`)
    setError(null)
    setFreed(null)
    try {
      await backend.startTransfer(m.folder, target)
      setFreed(`Copying ${m.repo_id} to ${target}; progress shows under Downloads.`)
      invalidate()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(null)
    }
  }

  const anyCache = nodes.some((n) => n.models)

  // The copies derate placed itself, per node, so each row says so.
  const replicaFolders = new Map<string, Map<string, boolean>>()
  for (const r of replication.data?.replicas ?? []) {
    const folders = replicaFolders.get(r.node_id) ?? new Map<string, boolean>()
    folders.set(r.folder, r.complete)
    replicaFolders.set(r.node_id, folders)
  }

  return (
    <div className="card2">
      <h3>Downloaded models</h3>
      <div className="unit" style={{ marginBottom: 'var(--space-10)' }}>
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
            copyTargets={n.node_id === coordinatorId ? targets : undefined}
            copyBlocked={n.node_id === coordinatorId ? blocked : undefined}
            onCopy={n.node_id === coordinatorId ? copy : undefined}
            replicaFolders={replicaFolders.get(n.node_id)}
            busy={busy}
          />
        ))
      )}

      {freed ? <div className="unit">{freed}</div> : null}
      {error ? (
        <div
          className="label"
          style={{ color: 'var(--fault)', marginTop: 'var(--space-8)', whiteSpace: 'pre-wrap' }}
        >
          {error}
        </div>
      ) : null}
    </div>
  )
}
