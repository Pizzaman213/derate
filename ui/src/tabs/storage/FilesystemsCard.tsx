import { useStorage } from '../../state/resources'
import { Verbatim } from '../../components/Verbatim'
import { ProportionBar } from '../../components/Bars'
import { Readout } from '../../components/Readout'
import { gbytes } from '../../format'
import type { Filesystem, NodeStorage } from '../../api/types'

/** Free space is the number an operator decides against, so it is the one that
 *  gets the readout width; the percentage is context for it. */
function tone(severity: Filesystem['severity']): 'ink' | 'warn' | 'fault' {
  return severity === 'critical' ? 'fault' : severity === 'warn' ? 'warn' : 'ink'
}

function NodeRow({ node }: { node: NodeStorage }) {
  // available:false is not an empty disk. Nothing numeric is drawn for it --
  // a 0 here reads as "full", and a dash plus the server's sentence is the
  // only honest rendering of "we could not look".
  if (!node.available) {
    return (
      <div className="row" style={{ alignItems: 'flex-start' }}>
        <span className="mono">{node.node_id}</span>
        <span style={{ color: 'var(--ink-muted)' }}>
          <Verbatim text={node.reason ?? 'Not measured.'} />
        </span>
      </div>
    )
  }

  if (!node.filesystems.length) {
    return (
      <div className="row">
        <span className="mono">{node.node_id}</span>
        <span className="unit">no filesystem could be measured</span>
      </div>
    )
  }

  return (
    <>
      {node.filesystems.map((fs, i) => (
        <div key={fs.device} style={{ marginBottom: 10 }}>
          <div
            style={{
              display: 'flex',
              alignItems: 'baseline',
              gap: 8,
              marginBottom: 4,
            }}
          >
            <span className="mono" style={{ minWidth: '14ch' }}>
              {i === 0 ? node.node_id : ''}
            </span>
            <Readout
              value={fs.free / 1024 ** 3}
              decimals={0}
              unit="GB free"
              width={6}
              size="readout"
              tone={tone(fs.severity)}
              title={`${gbytes(fs.free)} GB of ${gbytes(fs.total)} GB free`}
            />
            <span className="unit" style={{ marginLeft: 'auto' }}>
              {gbytes(fs.used, 0)} / {gbytes(fs.used + fs.free, 0)} GB ·{' '}
              {fs.used_pct}% used
            </span>
          </div>
          <ProportionBar
            value={fs.used_pct / 100}
            height={6}
            tone={tone(fs.severity)}
            label={`${node.node_id}: ${fs.used_pct}% of this filesystem used`}
          />
          <div className="unit" style={{ marginTop: 3 }}>
            {/* Naming every one of our paths that landed here is what shows an
                operator that two things they think are separate are not. */}
            {fs.mount_paths.join(' · ')}
            {fs.reserved > 0 ? ` · ${gbytes(fs.reserved, 0)} GB reserved for root` : ''}
          </div>
        </div>
      ))}
      {node.unreadable.map((u) => (
        <div className="unit" key={u.path} style={{ color: 'var(--warn)' }}>
          <Verbatim text={u.reason} />
        </div>
      ))}
    </>
  )
}

/** Capacity, per node, per filesystem.
 *
 *  A filesystem appears once however many of our paths sit on it: the data
 *  root, the resolver cache and the sparkrun cache are usually one disk, and
 *  listing them separately would report the same bytes three times. */
export function FilesystemsCard() {
  const storage = useStorage()
  const nodes = storage.data?.nodes ?? []

  return (
    <div className="card2">
      <h3>Filesystems</h3>
      <div className="unit" style={{ marginBottom: 10 }}>
        Capacity where this cluster keeps its data. Model weights are pulled by
        the runtime on each node and are not counted here — but they land on
        these filesystems, so this is the space a launch has to fit into.
      </div>

      {storage.loading ? (
        <div className="unit">Measuring…</div>
      ) : nodes.length === 0 ? (
        <div className="unit">No nodes in the cluster.</div>
      ) : (
        nodes.map((n) => <NodeRow key={n.node_id} node={n} />)
      )}

      {storage.error ? (
        <div
          className="label"
          style={{ color: 'var(--fault)', marginTop: 8, whiteSpace: 'pre-wrap' }}
        >
          {storage.error.message}
        </div>
      ) : null}
    </div>
  )
}
