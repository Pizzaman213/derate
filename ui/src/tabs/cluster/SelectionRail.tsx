import type { LinkMeasurement, NodeStateDTO, TopologyEdge } from '../../api/types'
import type { SafeMetricsFrame } from '../../state/useMetrics'
import { useSelection } from '../../state/selection'
import { nodeLive, nodeSignal } from '../../state/live'
import { Readout } from '../../components/Readout'
import { Lamp } from '../../components/Lamp'
import { ProportionBar } from '../../components/Bars'
import { Verbatim } from '../../components/Verbatim'
import { fmt, gbytes, pct, relativeTime } from '../../format'
import { edgeMeasured } from './layout'

const TP_THRESHOLD = 40

interface Props {
  edges: TopologyEdge[]
  measurements: LinkMeasurement[]
  nodes: NodeStateDTO[]
  frame: SafeMetricsFrame | null
  stale: boolean
  measuring: string | null
  measureError: { key: string; message: string } | null
  onMeasure: (a: string, b: string) => void
}

/** `fmt` plus its unit, dropped together -- a missing reading must not render
 *  as an em dash still wearing a unit it was never measured in ("— GB/s"). */
function fmtUnit(v: number | null | undefined, decimals: number, unit: string): string {
  const s = fmt(v, decimals)
  return s === '—' ? s : `${s} ${unit}`
}

function edgeKey(a: string, b: string): string {
  return [a, b].sort().join('~')
}
function findEdge(edges: TopologyEdge[], key: string): TopologyEdge | undefined {
  return edges.find((e) => edgeKey(e.src, e.dst) === key)
}
function findMeasurement(links: LinkMeasurement[], a: string, b: string): LinkMeasurement | undefined {
  return links.find((l) => (l.src === a && l.dst === b) || (l.src === b && l.dst === a))
}

/** The rail below the graph: three states, in order of how specific the
 *  selection is. Every field here is either a wire value or a straight
 *  aggregate over wire values -- nothing computed the way the mockup's own
 *  `curW()`/cost fields were. */
export function SelectionRail({
  edges,
  measurements,
  nodes,
  frame,
  stale,
  measuring,
  measureError,
  onMeasure,
}: Props) {
  const { selNode, selLink } = useSelection()

  if (selLink != null) {
    const edge = findEdge(edges, selLink)
    if (edge) {
      return (
        <LinkRail
          edge={edge}
          measurements={measurements}
          measuring={measuring}
          measureError={measureError}
          onMeasure={onMeasure}
        />
      )
    }
    // A bracket click always carries a valid pair (layout.ts always emits a
    // `linkKey` for a spanned deployment even with no topology edge record
    // yet), but the record itself may not exist -- a pair nothing has ever
    // touched, not even a `measured: false` placeholder. That is still an
    // unmeasured pair, so it gets the exact same rail, not a silent no-op.
    const [a, b] = selLink.split('~') as [string, string]
    return <UnmeasuredLinkRail a={a} b={b} measuring={measuring} measureError={measureError} onMeasure={onMeasure} />
  }

  if (selNode != null) {
    const node = nodes.find((n) => n.profile.node_id === selNode)
    if (node) return <NodeRail node={node} frame={frame} stale={stale} />
  }

  return <DefaultRail edges={edges} />
}

// ── Default: the chip list ──────────────────────────────────────────────────

function DefaultRail({ edges }: { edges: TopologyEdge[] }) {
  const { selectLink } = useSelection()
  const measured = edges.filter(edgeMeasured).length

  return (
    <div>
      <p className="unit" style={{ marginBottom: 8 }}>
        Click a node, or a pair below, for detail. {edges.length} node pairs · {measured} measured ·{' '}
        {edges.length - measured} never measured.
      </p>
      <div className="chips">
        {edges.map((e) => (
          <button key={edgeKey(e.src, e.dst)} onClick={() => selectLink(e.src, e.dst)}>
            {e.src} ↔ {e.dst}
            <span className="unit" style={{ marginLeft: 6 }}>
              {edgeMeasured(e) ? `${fmt(e.all_reduce_gbps, 1)} GB/s` : 'never measured'}
            </span>
          </button>
        ))}
      </div>
    </div>
  )
}

// ── A selected link ──────────────────────────────────────────────────────────

/** Shared by a known-but-unmeasured edge and a pair with no topology edge
 *  record at all -- both are, honestly, the same thing: nothing has ever
 *  been measured for this pair, so no number exists to show. */
function UnmeasuredLinkRail({
  a,
  b,
  measuring,
  measureError,
  onMeasure,
}: {
  a: string
  b: string
  measuring: string | null
  measureError: { key: string; message: string } | null
  onMeasure: (a: string, b: string) => void
}) {
  const key = edgeKey(a, b)
  const busy = measuring === key
  return (
    <div>
      <div className="sub" style={{ border: 'none', paddingTop: 0, marginTop: 0 }}>
        link · {a} ↔ {b}
      </div>
      <div className="unit">
        Never measured. No bandwidth figure is shown because none exists — the architecture&apos;s rule
        is that an unmeasured pair carries no numbers at all.
      </div>
      <div className="unit" style={{ marginTop: 8 }}>
        This saturates the link for about a minute.
      </div>
      <button style={{ marginTop: 8 }} onClick={() => onMeasure(a, b)} disabled={busy}>
        {busy ? 'Measuring…' : 'Measure this link'}
      </button>
      {!busy && measureError?.key === key ? (
        <div className="unit" style={{ marginTop: 8, color: 'var(--fault)' }}>
          {measureError.message}
        </div>
      ) : null}
    </div>
  )
}

function LinkRail({
  edge,
  measurements,
  measuring,
  measureError,
  onMeasure,
}: {
  edge: TopologyEdge
  measurements: LinkMeasurement[]
  measuring: string | null
  measureError: { key: string; message: string } | null
  onMeasure: (a: string, b: string) => void
}) {
  // Same definition of "measured" as the graph and the chips (edgeMeasured):
  // a measured:true edge with no figure must open the never-measured rail,
  // not a provenance grid asserting thresholds about a number that isn't there.
  if (!edgeMeasured(edge)) {
    return (
      <UnmeasuredLinkRail a={edge.src} b={edge.dst} measuring={measuring} measureError={measureError} onMeasure={onMeasure} />
    )
  }

  const m = findMeasurement(measurements, edge.src, edge.dst)
  const estimated = edge.estimated ?? m?.estimated ?? null
  const rawGbps = edge.raw_gbps ?? m?.raw_gbps ?? null
  const scaleFactor = edge.scale_factor ?? m?.scale_factor ?? null
  const notes = edge.notes ?? m?.notes ?? []
  const activePorts = edge.active_ports ?? m?.active_ports ?? null
  const totalPorts = edge.total_ports ?? m?.total_ports ?? null
  const portsOn = edge.ports_inspected_on ?? m?.ports_inspected_on ?? null
  const gdrBy = edge.gdr_detected_by ?? m?.gdr_detected_by ?? null
  // `TopologyEdge.gpudirect_rdma` is optional; `LinkMeasurement`'s is
  // required. Falling back only to the edge (as every other field here
  // falls back to the measurement) would render "disabled" for a link the
  // edge record simply hasn't populated yet, even when the underlying
  // measurement says otherwise -- so this, like every other field, checks
  // both, and an absence in both is a genuine "not known", not "disabled".
  const gdr = edge.gpudirect_rdma ?? m?.gpudirect_rdma ?? null
  const durationS = edge.duration_s ?? m?.duration_s ?? null
  const sendrecv = edge.sendrecv_gbps ?? m?.sendrecv_gbps ?? null
  const method = m?.method ?? null
  const measuredAt = m?.measured_at ?? null
  const ar = edge.all_reduce_gbps

  return (
    <div>
      <div className="sub" style={{ border: 'none', paddingTop: 0, marginTop: 0 }}>
        link · {edge.src} ↔ {edge.dst}
      </div>
      <div className="chartgrid" style={{ gridTemplateColumns: '1fr 1fr' }}>
        <div>
          <Row label="All-reduce" value={fmtUnit(ar, 1, 'GB/s')} />
          <Row label="Send/recv" value={fmtUnit(sendrecv, 1, 'GB/s')} />
          <Row label="Latency" value={fmtUnit(edge.latency_us, 0, 'µs')} />
          <Row label="Method" value={method ?? '—'} />
          {scaleFactor != null ? (
            <Row label="Derived" value={`${fmt(rawGbps, 1)} GB/s raw × ${scaleFactor} → ${fmt(ar, 1)} GB/s`} />
          ) : (
            <Row label="Source" value="measured directly" />
          )}
        </div>
        <div>
          <Row label="Estimated" value={estimated == null ? '—' : estimated ? 'yes — scaled, not observed' : 'no'} />
          <Row label="GPUDirect RDMA" value={gdr == null ? '—' : gdr ? 'enabled' : 'disabled'} />
          {activePorts != null && totalPorts != null && portsOn != null ? (
            <Row label="QSFP cages up" value={`${activePorts} of ${totalPorts} · read on ${portsOn}`} />
          ) : null}
          <Row label="Probe took" value={fmtUnit(durationS, 0, 's')} />
          <Row label="Measured" value={measuredAt != null ? relativeTime(measuredAt) : '—'} />
        </div>
      </div>
      {gdrBy ? <div className="unit" style={{ marginTop: 8 }}>{gdrBy}</div> : null}
      {notes.length > 0 ? (
        <div className="why on" style={{ marginTop: 8 }}>
          {notes.map((n, i) => (
            <div key={i}>{n}</div>
          ))}
        </div>
      ) : null}
      <div className="unit" style={{ marginTop: 8 }}>
        {ar != null && ar >= TP_THRESHOLD
          ? 'At or above the 40 GB/s threshold, so tensor parallel is viable across this pair.'
          : 'Below the 40 GB/s tensor-parallel threshold, which is why the planner chooses pipeline parallel over this pair.'}
      </div>
    </div>
  )
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="row">
      <span>{label}</span>
      <span className="mono">{value}</span>
    </div>
  )
}

// ── A selected node ──────────────────────────────────────────────────────────

function NodeRail({
  node,
  frame,
  stale,
}: {
  node: NodeStateDTO
  frame: SafeMetricsFrame | null
  stale: boolean
}) {
  const live = nodeLive(node, frame, stale)
  const signal = nodeSignal(node, live)
  const down = signal === 'fault'
  const grey = down || !live.fresh
  const p = node.profile
  const usable = Math.trunc(p.addressable_memory * 0.9)
  const allocatable = p.device_class === 'gb10' ? p.addressable_memory - node.memory_used : null

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <Lamp signal={signal} hollow={grey && !down} label={node.state} />
        <span className="sub" style={{ border: 'none', paddingTop: 0, marginTop: 0, marginBottom: 0 }}>
          {p.node_id}
        </span>
      </div>

      <div style={{ display: 'flex', gap: 'var(--s3)', flexWrap: 'wrap', marginTop: 10 }}>
        <Big label="power" value={live.power_w} unit="W" width={3} stale={grey} />
        <Big label="temperature" value={live.temp_c} unit="°C" width={3} stale={grey} />
        <Big label="GPU utilisation" value={live.util_pct} unit="%" width={3} stale={grey} />
        <Big label="memory used" value={live.memory_used_pct} unit="%" width={3} stale={grey} />
      </div>
      <ProportionBar
        value={live.memory_used_pct == null ? null : live.memory_used_pct / 100}
        height={6}
        tone={grey ? 'muted' : 'ink'}
        label={live.memory_used_pct == null ? 'no memory reading' : `${pct(live.memory_used_pct)} percent of addressable memory in use`}
      />

      <div className="row">
        <span>Addressable</span>
        <span className="mono">
          {gbytes(p.addressable_memory, 1)} GiB · {gbytes(usable, 1)} GiB usable at the 0.90 guardrail
        </span>
      </div>
      <div className="row">
        <span>Allocatable</span>
        <span className="mono">{allocatable != null ? `${gbytes(allocatable, 1)} GiB` : '—'}</span>
      </div>

      {node.eligible === false && node.ineligible_reason ? (
        <div style={{ marginTop: 8 }}>
          <Verbatim text={node.ineligible_reason} size="label" />
        </div>
      ) : null}

      {down && node.last_error ? (
        <div style={{ marginTop: 8 }}>
          <Verbatim text={node.last_error} size="label" />
        </div>
      ) : null}

      <div className="unit" style={{ marginTop: 8 }}>
        {p.device_class === 'gb10'
          ? 'Unified memory: the model and the operating system share one pool, so the static ceiling overstates what is actually allocatable. Only power, temperature, memory and GPU utilisation refresh live; the rest is read at fetch time.'
          : 'Discrete memory. Only power, temperature, memory and GPU utilisation refresh live.'}
      </div>
    </div>
  )
}

function Big({
  label,
  value,
  unit,
  width,
  stale,
}: {
  label: string
  value: number | null
  unit: string
  width: number
  stale: boolean
}) {
  return (
    <div style={{ display: 'grid', gap: 4 }}>
      <Readout value={value} decimals={0} width={width} unit={unit} size="readout" align="left" stale={stale} />
      <span className="unit">{label}</span>
    </div>
  )
}
