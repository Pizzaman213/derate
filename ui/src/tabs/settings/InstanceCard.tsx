import { useState } from 'react'
import { Verbatim } from '../../components/Verbatim'
import { relativeTime } from '../../format'
import { DeploymentLog } from '../../inspectors/DeploymentLog'
import { EventsAndLogs } from '../../inspectors/node/EventsAndLogs'
import { NodeLogFiles } from '../../inspectors/node/NodeLogFiles'
import { NodeRuntimeCard } from '../../inspectors/node/NodeRuntimeCard'
import { RequestsTable } from '../../inspectors/node/RequestsTable'
import { ResidentProcesses } from '../../inspectors/node/ResidentProcesses'
import { ServingBlock } from '../../inspectors/node/ServingBlock'
import { WindowChips } from '../../inspectors/node/WindowChips'
import type { HistoryWindow } from '../../state/history'
import { useMetrics } from '../../state/metrics'
import { fromState, nodeName } from '../../state/names'
import { useCluster, useRouting } from '../../state/resources'
import { useRouter } from '../../state/router'
import { useSelection } from '../../state/selection'
import { runningOrPrevious } from '../cluster/layout'

/** Settings -> Instance: pick a node and, optionally, a deployment on it, and
 *  see everything `NodeInspector` already shows for the pair -- reused
 *  directly here, not reimplemented -- plus this machine's own
 *  `node.log`/`proxy.log`, which nothing else in the product reads.
 *
 *  Rides `?node=`/`?dep=`, the same global selection every other screen
 *  shares, rather than owning selection state of its own: landing on
 *  `/settings?node=X&dep=Y` (see `SettingsTab.tsx`) opens straight onto this
 *  card with both selects already filled in, which is the whole point --
 *  that URL used to be indistinguishable from a plain `/settings`.
 *
 *  Deliberately narrower than the node sheet: `NodeCharts`, `HardwareRows`,
 *  `Interconnect` and `RenameNode` stay there, one click away via "Open the
 *  node sheet" below, along with the terminal. This card is about what an
 *  instance is DOING and what it has said, not the machine's hardware or its
 *  links to the rest of the floor. */
export function InstanceCard() {
  const cluster = useCluster()
  const routing = useRouting()
  const { frame } = useMetrics()
  const { route, navigate } = useRouter()
  const selection = useSelection()
  const [window, setWindow] = useState<HistoryWindow>('live')

  const nodes = cluster.data?.nodes ?? []
  const deployments = cluster.data?.deployments ?? []
  const node = nodes.find((n) => n.profile.node_id === route.node) ?? null
  const { here, previous } = node
    ? runningOrPrevious(deployments, node.profile.node_id)
    : { here: [], previous: undefined }
  const missingDep = route.dep != null && !here.some((d) => d.served_name === route.dep)

  return (
    <div className="card2">
      <h3>Instance</h3>
      <div className="unit" style={{ marginBottom: 10 }}>
        One machine, and what it is serving — the same panels the node sheet shows, reachable
        here without opening it.
      </div>

      <div
        style={{ display: 'flex', gap: 8, alignItems: 'flex-end', flexWrap: 'wrap', marginBottom: 14 }}
      >
        <div className="fld">
          <label htmlFor="instance-node">Node</label>
          <select
            id="instance-node"
            value={route.node ?? ''}
            onChange={(e) =>
              navigate({ node: e.target.value || null, dep: null }, { replace: true })
            }
          >
            <option value="">— choose a node —</option>
            {nodes.map((n) => (
              <option key={n.profile.node_id} value={n.profile.node_id}>
                {nodeName(fromState(n))}
              </option>
            ))}
          </select>
        </div>

        <div className="fld">
          <label htmlFor="instance-dep">Deployment</label>
          <select
            id="instance-dep"
            disabled={!node}
            value={route.dep ?? ''}
            onChange={(e) => navigate({ dep: e.target.value || null }, { replace: true })}
          >
            <option value="">(node only)</option>
            {here.map((d) => (
              <option key={d.deployment_id} value={d.served_name}>
                {d.served_name}
              </option>
            ))}
          </select>
        </div>

        {node ? <WindowChips value={window} onChange={setWindow} /> : null}
      </div>

      {route.node && !node ? (
        <div className="unit">
          '{route.node}' is not a node this coordinator knows about any more.
        </div>
      ) : null}
      {missingDep && node ? (
        <div className="unit" style={{ marginBottom: 10 }}>
          '{route.dep}' is not running on {nodeName(fromState(node))} right now.
        </div>
      ) : null}

      {!node ? null : (
        <div>
          <div
            style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}
          >
            <span className="label mono">{nodeName(fromState(node))}</span>
            <button
              onClick={() => selection.openSheet({ kind: 'node', id: node.profile.node_id })}
            >
              Open the node sheet
            </button>
          </div>

          <div className="sub" style={{ marginTop: 8 }}>
            serving
          </div>
          {here.length === 0 ? (
            previous ? (
              <div>
                <div className="unit" style={{ marginBottom: 6 }}>
                  Nothing now. {previous.served_name} was here, until it{' '}
                  {previous.state === 'failed' ? 'failed' : 'stopped'}
                  {previous.started_at != null
                    ? ` (started ${relativeTime(previous.started_at)})`
                    : ''}
                  .
                </div>
                {previous.last_error ? <Verbatim text={previous.last_error} size="label" /> : null}
                <DeploymentLog deploymentId={previous.deployment_id} autoOpen={false} />
              </div>
            ) : (
              <div className="unit">Nothing.</div>
            )
          ) : (
            here.map((d) => (
              <ServingBlock
                key={d.deployment_id}
                dep={d}
                cfg={routing.data?.find((c) => c.served_name === d.served_name) ?? null}
                frame={frame}
                window={window}
              />
            ))
          )}

          <div className="sub">requests that ran here</div>
          <RequestsTable nodeId={node.profile.node_id} window={window} />

          <ResidentProcesses nodeId={node.profile.node_id} />

          <NodeRuntimeCard nodeId={node.profile.node_id} />

          <EventsAndLogs nodeId={node.profile.node_id} window={window} />

          <NodeLogFiles nodeId={node.profile.node_id} />
        </div>
      )}
    </div>
  )
}
