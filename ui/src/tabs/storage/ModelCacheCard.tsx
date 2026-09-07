import { useState } from 'react'
import { useStorage, useCluster } from '../../state/resources'
import { useBackend } from '../../state/backend'
import { ProportionBar } from '../../components/Bars'
import { Verbatim } from '../../components/Verbatim'
import { relativeTime } from '../../format'
import type { CachedModel, NodeStorage } from '../../api/types'

const GiB = 1024 ** 3

function sizeLabel(b: number | null | undefined): string {
  if (b == null) return '—'
  if (b >= GiB) return `${(b / GiB).toFixed(1)} GB`
  if (b >= 1024 ** 2) return `${(b / 1024 ** 2).toFixed(0)} MB`
  return `${(b / 1024).toFixed(0)} KB`
}

/** Encoded folder name for a served model, so the "in use" marking in the
 *  browser matches the one the server refuses on. Decoding the other way is
 *  ambiguous whenever a model name contains a double hyphen. */
function folderFor(modelId: string): string {
  return `models--${modelId.trim().replace(/\//g, '--')}`
}

function NodeCache({
  node,
  servedFolders,
  onDelete,
  busy,
}: {
  node: NodeStorage
  servedFolders: Set<string>
  onDelete: (nodeId: string, m: CachedModel) => void
  busy: string | null
}) {
  const cache = node.models

  if (!cache) return null

  if (!cache.available) {
    return (
      <div style={{ marginBottom: 12 }}>
        <div className="row">
          <span className="mono">{node.node_id}</span>
          <span className="unit">not measured</span>
        </div>
        {cache.reason ? <Verbatim text={cache.reason} size="label" /> : null}
      </div>
    )
  }

  if (!cache.repos.length) {
    return (
      <div className="row" style={{ marginBottom: 12 }}>
        <span className="mono">{node.node_id}</span>
        <span className="unit">nothing downloaded · {cache.path}</span>
      </div>
    )
  }

  // Proportion is against the largest repository, not the total: this is a
  // ranking of what is worth deleting, and against a 894 GB total every bar
  // but the first would be invisible.
  const largest = cache.repos[0]?.bytes ?? 1

  return (
    <div style={{ marginBottom: 14 }}>
      <div
        className="row"
        style={{ borderBottom: '1px solid var(--rule)', paddingBottom: 4 }}
      >
        <span className="mono">{node.node_id}</span>
        <span className="unit">
          {cache.repos.length} models · {sizeLabel(cache.total_bytes)} · {cache.path}
        </span>
      </div>

      <div style={{ overflowX: 'auto' }}>
        <table>
          <thead>
            <tr>
              <th>Model</th>
              <th style={{ width: 90 }}></th>
              <th style={{ textAlign: 'right' }}>Size</th>
              <th>Downloaded</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {cache.repos.map((m) => {
              const inUse = servedFolders.has(m.folder)
              const key = `${node.node_id}/${m.folder}`
              return (
                <tr key={m.folder}>
                  <td className="mono" title={m.folder}>
                    {m.repo_id}
                    {m.revisions.length > 1 ? (
                      <span className="unit"> · {m.revisions.length} revisions</span>
                    ) : null}
                  </td>
                  <td>
                    <ProportionBar
                      value={m.bytes / largest}
                      width={80}
                      height={5}
                      tone={inUse ? 'live' : 'ink'}
                      label={`${m.repo_id}: ${sizeLabel(m.bytes)}`}
                    />
                  </td>
                  <td className="num">{sizeLabel(m.bytes)}</td>
                  <td className="unit">
                    {m.last_modified ? relativeTime(m.last_modified) : '—'}
                  </td>
                  <td style={{ textAlign: 'right' }}>
                    {inUse ? (
                      // Marked, not merely disabled: the server refuses this
                      // with a 409, and the reason is the useful half.
                      <span className="unit" style={{ color: 'var(--live)' }}>
                        serving
                      </span>
                    ) : (
                      <button
                        onClick={() => onDelete(node.node_id, m)}
                        disabled={busy != null}
                      >
                        {busy === key ? 'Deleting…' : 'Delete'}
                      </button>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
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
          <NodeCache
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
