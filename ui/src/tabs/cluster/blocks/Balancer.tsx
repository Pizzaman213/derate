// design-pack: data-svg — the load balancer's two triangles are chrome drawn
// to the socket geometry: requests enter at the split's point, local and
// cloud leave its flat side, and the merge mirrors it.
import type { NodeProps } from '@xyflow/react'
import { Icon } from '../../../components/Icon'
import { Socket, socket, toneOf, useEditor } from './editor'
import type { BlockNode } from './Terminals'

function stroke(selected: boolean, tone: 'fault' | 'warn' | null): string {
  if (tone === 'fault') return 'var(--graph-fault)'
  if (tone === 'warn') return 'var(--graph-warn)'
  return selected ? 'var(--graph-block-selected)' : 'var(--graph-block-border)'
}

/** ◁ One served name's load balancer, as it splits: requests in at the point,
 *  `local` then `cloud` out of the flat side, its `limit` on the lower edge.
 *  Double-click opens its dialog (policy, limits, targets, removal). */
export function SplitBlock({ data, selected }: NodeProps<BlockNode<'split'>>) {
  const editor = useEditor()
  const b = data.block
  // A pending removal draws it amber until Save takes it away.
  const tone = toneOf(editor.issuesFor(b.id)) ?? (b.removing ? 'warn' : null)
  const limit = b.sockets.find((s) => s.socket === 'limit')
  return (
    <div className="wnode wtri" role="group" aria-label={b.title} title="Double-click to configure this load balancer">
      <svg className="wtri-shape" viewBox="0 0 200 120" preserveAspectRatio="none" aria-hidden="true">
        <polygon
          points="2,60 198,2 198,118"
          fill="var(--graph-block-bg)"
          stroke={stroke(selected, tone)}
          strokeWidth={selected || tone ? 2 : 1}
          vectorEffect="non-scaling-stroke"
          strokeDasharray={b.draft ? '4 3' : undefined}
        />
      </svg>
      <div className="wtri-text wtri-text-split">
        <span className="label" title={b.served}>
          {b.served}
        </span>
        <span className="unit">
          <Icon name="route" size={12} decorative />{' '}
          {b.removing ? 'removed on Save' : b.draft ? 'not served yet' : 'load balancer'}
        </span>
      </div>
      {b.policyChip ? <span className="pill wtri-chip">{b.policyChip}</span> : null}
      <span className="socklabel wtri-port wtri-port-local" style={{ color: 'var(--graph-req)' }}>
        {b.ports.local}
      </span>
      <span className="socklabel wtri-port wtri-port-cloud" style={{ color: 'var(--graph-req)' }}>
        {b.ports.cloud}
      </span>
      {limit ? (
        <span className="socklabel wtri-limit-label" style={{ color: 'var(--graph-limit)' }}>
          limit
        </span>
      ) : null}
      {/* In at the point (left), out of the flat side (right), the limit on
          the lower slanted edge: (2,60)->(198,118) crosses x=100 at y=89,
          74% down the box. */}
      <Socket spec={socket(b.sockets, 'in')} style={{ top: '50%' }} />
      <Socket spec={socket(b.sockets, 'local')} style={{ top: '25%' }} />
      <Socket spec={socket(b.sockets, 'cloud')} style={{ top: '75%' }} />
      {limit ? <Socket spec={limit} style={{ left: '50%', top: '74%', bottom: 'auto', transform: 'translate(-50%, -50%)' }} /> : null}
    </div>
  )
}

/** ▷ The same served name's answers coming back together: `local` above,
 *  `cloud` below, out at the point to the API Output. */
export function MergeBlock({ data, selected }: NodeProps<BlockNode<'merge'>>) {
  const b = data.block
  return (
    <div className="wnode wtri" role="group" aria-label={b.title} title="Double-click to configure this load balancer">
      <svg className="wtri-shape" viewBox="0 0 200 120" preserveAspectRatio="none" aria-hidden="true">
        <polygon
          points="2,2 2,118 198,60"
          fill="var(--graph-block-bg)"
          stroke={stroke(selected, null)}
          strokeWidth={selected ? 2 : 1}
          vectorEffect="non-scaling-stroke"
        />
      </svg>
      <div className="wtri-text wtri-text-merge">
        <span className="label" title={b.served}>
          {b.served}
        </span>
        <span className="unit">merge</span>
      </div>
      <span className="socklabel wtri-port wtri-port-in-local" style={{ color: 'var(--graph-resp)' }}>
        local
      </span>
      <span className="socklabel wtri-port wtri-port-in-cloud" style={{ color: 'var(--graph-resp)' }}>
        cloud
      </span>
      <span className="socklabel wtri-port wtri-port-out" style={{ color: 'var(--graph-resp)' }}>
        responses
      </span>
      <Socket spec={socket(b.sockets, 'local')} style={{ top: '25%' }} />
      <Socket spec={socket(b.sockets, 'cloud')} style={{ top: '75%' }} />
      <Socket spec={socket(b.sockets, 'out')} style={{ top: '50%' }} />
    </div>
  )
}
