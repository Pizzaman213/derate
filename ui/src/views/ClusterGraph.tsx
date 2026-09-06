import { useMemo, useState } from 'react'
import type {
  MetricsFrame,
  RoutingConfig,
  Topology,
  TopologyEdge,
  TopologyNode,
} from '../api/types'
import {
  BAND_GAP,
  BAND_H,
  BOX_H,
  BOX_W,
  MARGIN,
  edgeGeometry,
  edgeWidth,
  layoutNodes,
  type Placed,
} from './layout'
import { fmt, gbytes, shortGpu } from '../format'

interface Props {
  topology: Topology
  frame: MetricsFrame | null
  stale: boolean
  routing: RoutingConfig[]
  measuring: string | null
  onSelectNode: (nodeId: string) => void
  onMeasure: (a: string, b: string) => void
}

const edgeKey = (e: { src: string; dst: string }) =>
  [e.src, e.dst].sort().join('~')

export function ClusterGraph({
  topology,
  frame,
  stale,
  routing,
  measuring,
  onSelectNode,
  onMeasure,
}: Props) {
  const [selectedEdge, setSelectedEdge] = useState<string | null>(null)

  const layout = useMemo(
    () => layoutNodes(topology.nodes.map((n) => n.node_id)),
    [topology.nodes],
  )
  const placed = useMemo(() => {
    const m = new Map<string, Placed>()
    layout.nodes.forEach((p) => m.set(p.node_id, p))
    return m
  }, [layout])

  // Only deployments that occupy more than one machine become bands. A single
  // node deployment is drawn inside the machine that runs it.
  const bands = topology.deployments.filter((d) => d.node_ids.length > 1)
  const height =
    layout.headerHeight +
    (bands.length ? bands.length * (BAND_H + BAND_GAP) + BAND_LEAD_SPACE : 0) +
    MARGIN

  const edge = selectedEdge
    ? topology.edges.find((e) => edgeKey(e) === selectedEdge)
    : undefined

  return (
    <div style={{ display: 'grid', gap: 'var(--s2)' }}>
      <div style={{ overflowX: 'auto' }}>
        <svg
          width={layout.width}
          height={height}
          viewBox={`0 0 ${layout.width} ${height}`}
          style={{ display: 'block', maxWidth: '100%', height: 'auto' }}
          role="img"
          aria-label={`Cluster graph: ${topology.nodes.length} machines, ${topology.edges.length} links`}
        >
          {/* Edges first, so machines sit on top of them. */}
          {topology.edges.map((e) => {
            const a = placed.get(e.src)
            const b = placed.get(e.dst)
            if (!a || !b) return null
            return (
              <Edge
                key={edgeKey(e)}
                edge={e}
                a={a}
                b={b}
                kind={layout.kind}
                selected={selectedEdge === edgeKey(e)}
                onSelect={() =>
                  setSelectedEdge((cur) =>
                    cur === edgeKey(e) ? null : edgeKey(e),
                  )
                }
              />
            )
          })}

          {bands.map((d, i) => (
            <Band
              key={d.deployment_id}
              deployment={d}
              placed={placed}
              layout={layout}
              index={i}
              tps={
                frame?.deployments.find(
                  (x) => x.deployment_id === d.deployment_id,
                )?.tokens_per_sec ?? d.tokens_per_sec
              }
              stale={stale}
            />
          ))}

          {topology.nodes.map((n) => {
            const p = placed.get(n.node_id)
            if (!p) return null
            return (
              <NodeBox
                key={n.node_id}
                node={n}
                at={p}
                frame={frame}
                stale={stale}
                share={weightedShare(routing, n.node_id)}
                singleNodeDeployments={topology.deployments
                  .filter(
                    (d) =>
                      d.node_ids.length === 1 && d.node_ids[0] === n.node_id,
                  )
                  .map((d) => d.served_name)}
                onSelect={() => onSelectNode(n.node_id)}
              />
            )
          })}
        </svg>
      </div>

      <p className="unit" style={{ margin: 0 }}>
        Link thickness is the measured all-reduce bandwidth against the 40 GB/s
        tensor-parallel threshold. A dashed link has never been probed and
        carries no figure.
      </p>

      {edge ? (
        <EdgeDetail
          edge={edge}
          measuring={measuring}
          onMeasure={onMeasure}
          onClose={() => setSelectedEdge(null)}
        />
      ) : null}
    </div>
  )
}

/** Gap between the machine row and the first deployment band. */
const BAND_LEAD_SPACE = 26

// ── Machines ─────────────────────────────────────────────────────────────────

function NodeBox({
  node,
  at,
  frame,
  stale,
  share,
  singleNodeDeployments,
  onSelect,
}: {
  node: TopologyNode
  at: Placed
  frame: MetricsFrame | null
  stale: boolean
  share: { served_name: string; weight: number } | null
  singleNodeDeployments: string[]
  onSelect: () => void
}) {
  const f = frame?.nodes.find((n) => n.node_id === node.node_id)
  const down = node.state === 'unreachable'
  const live = !stale && !down && f != null && f.power_w != null
  const mem = (live ? f?.memory_used_pct : null) ?? node.memory_used_pct
  const power = (live ? f?.power_w : null) ?? node.power_w

  const signal =
    down ? 'var(--fault)' : node.state === 'degraded' ? 'var(--warn)' : 'var(--live)'
  const dim = down || !live
  const inkTone = dim ? 'var(--ink-muted)' : 'var(--ink)'

  return (
    <g
      transform={`translate(${at.x} ${at.y})`}
      onClick={onSelect}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          onSelect()
        }
      }}
      tabIndex={0}
      role="button"
      aria-label={`${node.hostname}, ${node.state}, ${Math.round(mem)} percent memory used`}
      style={{ cursor: 'pointer' }}
    >
      <rect width={BOX_W} height={BOX_H} fill="var(--panel)" />
      {/* Fill is memory used. The border carries the signal. */}
      <rect
        width={(BOX_W * Math.max(0, Math.min(100, mem))) / 100}
        height={BOX_H}
        fill="var(--ink)"
        opacity={0.07}
      />
      <rect
        width={BOX_W}
        height={BOX_H}
        fill="none"
        stroke={signal}
        strokeWidth={1.5}
      />

      <text x={12} y={24} fill={inkTone} fontFamily="var(--font-sans)" fontSize={14} fontWeight={500}>
        {node.hostname}
      </text>
      {node.role === 'coordinator' ? (
        <text
          x={BOX_W - 12}
          y={24}
          textAnchor="end"
          fill="var(--ink-muted)"
          fontFamily="var(--font-sans)"
          fontSize={11}
        >
          coordinator
        </text>
      ) : null}

      <text x={12} y={42} fill="var(--ink-muted)" fontFamily="var(--font-sans)" fontSize={12}>
        {shortGpu(node.gpu_name)}
        {node.total_memory ? ` · ${gbytes(node.total_memory, 0)} GB` : ''}
      </text>

      <circle cx={16} cy={62} r={4} fill={dim && !down ? 'none' : signal} stroke={signal} strokeWidth={1.5} />
      {/* Values are right-anchored against a fixed x, so a reading going from
          two digits to three moves the digits and never the unit. */}
      <text
        x={62}
        y={66}
        textAnchor="end"
        fill={inkTone}
        fontFamily="var(--font-mono)"
        fontSize={13}
        style={{ fontVariantNumeric: 'tabular-nums' }}
      >
        {fmt(mem, 0)}
      </text>
      <text x={66} y={66} fill="var(--ink-muted)" fontFamily="var(--font-sans)" fontSize={11}>
        %
      </text>
      <text
        x={124}
        y={66}
        textAnchor="end"
        fill={inkTone}
        fontFamily="var(--font-mono)"
        fontSize={13}
        style={{ fontVariantNumeric: 'tabular-nums' }}
      >
        {fmt(power, 0)}
      </text>
      <text x={128} y={66} fill="var(--ink-muted)" fontFamily="var(--font-sans)" fontSize={11}>
        W
      </text>

      {singleNodeDeployments.length > 0 ? (
        <text x={12} y={86} fill={inkTone} fontFamily="var(--font-sans)" fontSize={12}>
          {singleNodeDeployments[0]}
          {singleNodeDeployments.length > 1
            ? ` +${singleNodeDeployments.length - 1}`
            : ''}
        </text>
      ) : null}

      {/* Under weighted routing, an unequal split should be visible rather than
          mysterious. */}
      {share ? (
        <g>
          <rect x={12} y={96} width={BOX_W - 60} height={4} fill="var(--panel-recessed)" stroke="var(--rule)" strokeWidth={0.5} />
          <rect
            x={12}
            y={96}
            width={(BOX_W - 60) * Math.max(0, Math.min(1, share.weight))}
            height={4}
            fill={share.weight === 0 ? 'var(--ink-muted)' : 'var(--ink)'}
          />
          <text
            x={BOX_W - 12}
            y={101}
            textAnchor="end"
            fill="var(--ink-muted)"
            fontFamily="var(--font-mono)"
            fontSize={11}
            style={{ fontVariantNumeric: 'tabular-nums' }}
          >
            {Math.round(share.weight * 100)}%
          </text>
          <title>{`${share.served_name}: ${Math.round(share.weight * 100)} percent of traffic`}</title>
        </g>
      ) : null}
    </g>
  )
}

/** The share of traffic this machine takes, when a served name is routing by
 *  weighted capacity. Zero is a real answer and is drawn, not hidden. */
function weightedShare(
  configs: RoutingConfig[],
  nodeId: string,
): { served_name: string; weight: number } | null {
  for (const cfg of configs) {
    if (cfg.policy !== 'weighted_capacity') continue
    const t = cfg.targets.find(
      (x) => x.kind === 'local' && x.node_ids?.includes(nodeId),
    )
    if (t) return { served_name: cfg.served_name, weight: t.weight }
  }
  return null
}

// ── Links ────────────────────────────────────────────────────────────────────

function Edge({
  edge,
  a,
  b,
  kind,
  selected,
  onSelect,
}: {
  edge: TopologyEdge
  a: Placed
  b: Placed
  kind: 'row' | 'ring'
  selected: boolean
  onSelect: () => void
}) {
  const geo = edgeGeometry(a, b, kind)
  const measured = edge.measured && edge.all_reduce_gbps != null
  const width = measured ? edgeWidth(edge.all_reduce_gbps!) : 1.5

  // An unmeasured link is drawn, because it exists, but it never carries a
  // number we did not measure.
  const label = measured ? `${fmt(edge.all_reduce_gbps, 1)} GB/s` : 'measure'
  const labelW = label.length * 6.6 + 10

  return (
    <g
      onClick={onSelect}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          onSelect()
        }
      }}
      tabIndex={0}
      role="button"
      aria-label={
        measured
          ? `Link ${edge.src} to ${edge.dst}, ${edge.all_reduce_gbps} gigabytes per second all-reduce over ${edge.medium}`
          : `Link ${edge.src} to ${edge.dst} over ${edge.medium}, never measured`
      }
      style={{ cursor: 'pointer' }}
    >
      {/* A fat transparent hit area, so a 2px line is still clickable. */}
      <path d={geo.d} fill="none" stroke="transparent" strokeWidth={16} />
      <path
        d={geo.d}
        fill="none"
        stroke={measured ? 'var(--ink)' : 'var(--ink-muted)'}
        strokeWidth={width}
        strokeDasharray={measured ? undefined : '4 4'}
        opacity={measured ? 0.85 : 0.7}
      />
      <rect
        x={geo.mid.x - labelW / 2}
        y={geo.mid.y - 9}
        width={labelW}
        height={18}
        fill="var(--panel)"
        stroke={selected ? 'var(--ink)' : 'none'}
        strokeWidth={1}
        rx={2}
      />
      <text
        x={geo.mid.x}
        y={geo.mid.y + 4}
        textAnchor="middle"
        fill={measured ? 'var(--ink)' : 'var(--ink-muted)'}
        fontFamily={measured ? 'var(--font-mono)' : 'var(--font-sans)'}
        fontSize={11}
        style={{ fontVariantNumeric: 'tabular-nums' }}
      >
        {label}
      </text>
    </g>
  )
}

function EdgeDetail({
  edge,
  measuring,
  onMeasure,
  onClose,
}: {
  edge: TopologyEdge
  measuring: string | null
  onMeasure: (a: string, b: string) => void
  onClose: () => void
}) {
  const busy = measuring === [edge.src, edge.dst].sort().join('~')
  const measured = edge.measured && edge.all_reduce_gbps != null

  return (
    <div
      style={{
        border: '1px solid var(--rule)',
        background: 'var(--panel-recessed)',
        padding: 'var(--s1)',
        display: 'grid',
        gap: 8,
      }}
    >
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'baseline',
          gap: 8,
        }}
      >
        <span style={{ fontWeight: 500 }}>
          {edge.src} to {edge.dst}
        </span>
        <button onClick={onClose} className="label" style={{ border: 0, color: 'var(--ink-muted)' }}>
          close
        </button>
      </div>

      {measured ? (
        <dl
          style={{
            margin: 0,
            display: 'grid',
            gridTemplateColumns: 'auto 1fr',
            columnGap: 'var(--s2)',
            rowGap: 4,
          }}
        >
          <Row label="medium" value={edge.medium} />
          <Row label="all-reduce" value={`${fmt(edge.all_reduce_gbps, 1)} GB/s`} mono />
          <Row label="send/recv" value={`${fmt(edge.sendrecv_gbps, 1)} GB/s`} mono />
          <Row label="latency" value={`${fmt(edge.latency_us, 0)} µs`} mono />
          <Row
            label="GPUDirect RDMA"
            value={edge.gpudirect_rdma ? 'on' : 'off'}
          />
        </dl>
      ) : (
        <p className="label" style={{ fontWeight: 400, margin: 0 }}>
          Never probed. This link is drawn because it exists, not because we know
          how fast it is.
        </p>
      )}

      {!edge.gpudirect_rdma && measured ? (
        <p className="label muted" style={{ fontWeight: 400, margin: 0 }}>
          RDMA is off, which is most of why this is below the nameplate figure.
        </p>
      ) : null}

      <p className="label muted" style={{ fontWeight: 400, margin: 0 }}>
        Measuring saturates the link for about 20 seconds and will slow anything
        serving over it.
      </p>
      <div>
        <button onClick={() => onMeasure(edge.src, edge.dst)} disabled={busy}>
          {busy ? 'Measuring…' : measured ? 'Measure again' : 'Measure link'}
        </button>
      </div>
    </div>
  )
}

function Row({
  label,
  value,
  mono,
}: {
  label: string
  value: string
  mono?: boolean
}) {
  return (
    <>
      <dt className="label muted" style={{ fontWeight: 400 }}>
        {label}
      </dt>
      <dd
        className={mono ? 'mono' : undefined}
        style={{ margin: 0, fontSize: mono ? 13 : undefined }}
      >
        {value}
      </dd>
    </>
  )
}

// ── Deployment bands ─────────────────────────────────────────────────────────

function Band({
  deployment,
  placed,
  layout,
  index,
  tps,
  stale,
}: {
  deployment: Topology['deployments'][number]
  placed: Map<string, Placed>
  layout: ReturnType<typeof layoutNodes>
  index: number
  tps: number | null
  stale: boolean
}) {
  const occupied = deployment.node_ids
    .map((id) => placed.get(id))
    .filter((p): p is Placed => p != null)
  if (occupied.length === 0) return null

  const top = layout.headerHeight + BAND_LEAD_SPACE + index * (BAND_H + BAND_GAP)

  // On a ring the band cannot span geometrically, so it runs the full width and
  // names the machines instead.
  const left =
    layout.kind === 'ring' ? MARGIN : Math.min(...occupied.map((p) => p.x))
  const right =
    layout.kind === 'ring'
      ? layout.width - MARGIN
      : Math.max(...occupied.map((p) => p.x + BOX_W))

  const degraded = deployment.state === 'degraded'
  const tone = degraded ? 'var(--warn)' : 'var(--live)'

  return (
    <g aria-label={`${deployment.served_name} on ${deployment.node_ids.join(' and ')}`}>
      {/* Leads from each occupied machine down to the band. */}
      {layout.kind === 'row'
        ? occupied.map((p) => (
            <path
              key={p.node_id}
              d={`M${p.x + BOX_W / 2} ${p.y + BOX_H} L${p.x + BOX_W / 2} ${top}`}
              stroke="var(--rule)"
              strokeWidth={1}
              fill="none"
            />
          ))
        : null}

      <rect
        x={left}
        y={top}
        width={right - left}
        height={BAND_H}
        fill="var(--panel-recessed)"
        stroke={tone}
        strokeWidth={1}
      />
      <text
        x={left + 12}
        y={top + 17}
        fill="var(--ink)"
        fontFamily="var(--font-sans)"
        fontSize={13}
        fontWeight={500}
      >
        {deployment.served_name}
      </text>
      <text
        x={left + 12}
        y={top + 32}
        fill="var(--ink-muted)"
        fontFamily="var(--font-sans)"
        fontSize={11}
      >
        {deployment.plan}
        {layout.kind === 'ring' ? ` · ${deployment.node_ids.join(', ')}` : ''}
        {degraded ? ' · degraded' : ''}
      </text>
      <text
        x={right - 12}
        y={top + 26}
        textAnchor="end"
        fill={stale ? 'var(--ink-muted)' : 'var(--ink)'}
        fontFamily="var(--font-mono)"
        fontSize={17}
        fontWeight={500}
        style={{ fontVariantNumeric: 'tabular-nums' }}
      >
        {fmt(tps, 1)}
      </text>
      <text
        x={right - 12}
        y={top + 38}
        textAnchor="end"
        fill="var(--ink-muted)"
        fontFamily="var(--font-sans)"
        fontSize={10}
      >
        tok/s
      </text>
    </g>
  )
}
