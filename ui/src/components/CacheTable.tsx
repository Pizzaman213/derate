import { ProportionBar } from './Bars'
import { Verbatim } from './Verbatim'
import { relativeTime } from '../format'
import type { CachedModel, NodeStorage } from '../api/types'

const GiB = 1024 ** 3

function sizeLabel(b: number | null | undefined): string {
  if (b == null) return '—'
  if (b >= GiB) return `${(b / GiB).toFixed(1)} GB`
  if (b >= 1024 ** 2) return `${(b / 1024 ** 2).toFixed(0)} MB`
  return `${(b / 1024).toFixed(0)} KB`
}

/** One node's downloaded weights, largest first.
 *
 *  Shared by Settings → Storage, which can delete a repository, and the
 *  Models screen's read-only summary, which cannot: `servedFolders`,
 *  `onDelete` and `busy` are all optional, and the actions column simply
 *  does not render without them. `inUse` degrades to "unknown" rather than
 *  "not in use" when `servedFolders` is absent, because the proportion bar
 *  otherwise has no honest tone to draw. */
export function CacheTable({
  node,
  servedFolders,
  onDelete,
  busy,
}: {
  node: NodeStorage
  servedFolders?: Set<string>
  onDelete?: (nodeId: string, m: CachedModel) => void
  busy?: string | null
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
              {onDelete ? <th></th> : null}
            </tr>
          </thead>
          <tbody>
            {cache.repos.map((m) => {
              const inUse = servedFolders?.has(m.folder) ?? false
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
                  {onDelete ? (
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
                  ) : null}
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}
