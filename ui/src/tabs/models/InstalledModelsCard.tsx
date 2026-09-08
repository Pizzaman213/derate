import { CacheTable } from '../../components/CacheTable'
import type { StorageReport } from '../../api/types'

/** What is already on the cluster's disks, read-only.
 *
 *  Same figures as Settings → Storage's own card, because it reads the same
 *  `/api/storage` report -- handed down rather than polled again, for the
 *  reason `providers`/`providerKinds` are (`ModelsTab.tsx`): a second interval
 *  asking for one payload this tab already has. Deleting a cached model stays
 *  a Settings → Storage action; this only says what is there. */
export function InstalledModelsCard({
  storage,
  loading,
  error,
}: {
  storage: StorageReport | null
  loading: boolean
  error: Error | null
}) {
  const nodes = storage?.nodes ?? []
  const anyCache = nodes.some((n) => n.models)

  return (
    <div className="card2">
      <h3>Installed models</h3>
      <div className="unit" style={{ marginBottom: 10 }}>
        Model weights already downloaded onto this cluster, per node. Freed or
        deleted from Settings → Storage.
      </div>

      {loading ? (
        <div className="unit">Measuring…</div>
      ) : !anyCache ? (
        <div className="unit">No node reported a model cache.</div>
      ) : (
        nodes.map((n) => <CacheTable key={n.node_id} node={n} />)
      )}

      {error ? (
        <div
          className="label"
          style={{ color: 'var(--fault)', marginTop: 8, whiteSpace: 'pre-wrap' }}
        >
          {error.message}
        </div>
      ) : null}
    </div>
  )
}
