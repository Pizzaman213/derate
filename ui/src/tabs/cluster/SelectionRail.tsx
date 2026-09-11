import type { LinkMeasurement, LinkTuning, ReachReport, TopologyEdge } from '../../api/types'
import { useSelection } from '../../state/selection'
import { fmt, fmtUnit, relativeTime } from '../../format'
// One spelling of the threshold and of a pair's key, both layout.ts's: a
// second copy here is how the rail's verdict sentence and the wire's own
// scale drift apart.
import { edgeKey, edgeMeasured, TP_THRESHOLD } from './layout'
// The tuning row's decisions live in a plain module so a verifier can reach
// them: `check.mjs` bundles with esbuild `platform: 'neutral'` and cannot
// import anything that pulls in React.
import { canTune, tuningEvidence, tuningLabel, tuningState } from './tuning'

/** Everything the two link rails need to run and show a reachability check.
 *  Passed as one object because both rails take all of it and none of it means
 *  anything on its own. */
export interface ReachState {
  /** Pair key currently being checked, or null. */
  checking: string | null
  /** The last report, and which pair it was about. Kept keyed so a report for
   *  one link never renders under another. */
  report: { key: string; value: ReachReport } | null
  error: { key: string; message: string } | null
  onCheck: (a: string, b: string) => void
}

interface Props {
  edges: TopologyEdge[]
  measurements: LinkMeasurement[]
  measuring: string | null
  measureError: { key: string; message: string } | null
  onMeasure: (a: string, b: string) => void
  /** What to call a node. Ids are what the wire carries and what the graph is
   *  keyed by; this is the only thing that turns one into a caption, so the
   *  chips here and the plates above cannot disagree about a machine's name. */
  name: (id: string) => string
  reach: ReachState
  /** The calibration action, bundled like `reach` and for the same reason: a
   *  different question at a different price, and one running must not grey
   *  out the other's button. `error` is keyed by pair so it never renders
   *  under a link it does not belong to. */
  tune: {
    error: { key: string; message: string } | null
    onTune: (a: string, b: string) => void
    /** Size bands the evidence lines are read at, from the server's own
     *  constants rather than a second copy here. */
    decodeBand: number
    bulkBand: number
  }
  /** Above four machines the graph stops drawing the whole unmeasured mesh --
   *  66 pairs at twelve machines buries the one link that carries a figure.
   *  The chips below are the complete list either way, so this only changes
   *  what the hint says, never what is listed. */
  crowded: boolean
}

function findEdge(edges: TopologyEdge[], key: string): TopologyEdge | undefined {
  return edges.find((e) => edgeKey(e.src, e.dst) === key)
}
function findMeasurement(links: LinkMeasurement[], a: string, b: string): LinkMeasurement | undefined {
  return links.find((l) => (l.src === a && l.dst === b) || (l.src === b && l.dst === a))
}

/** The rail below the graph: a selected link, or the chip list. A machine is
 *  deliberately NOT one of them -- pressing a plate only lights it, and its
 *  readouts live in the node sheet a double-click opens (NodeInspector),
 *  which carries the same four live figures and everything else about the
 *  machine besides. One detail surface, one way into it.
 *
 *  Every field here is either a wire value or a straight aggregate over wire
 *  values -- nothing computed the way the mockup's own `curW()`/cost fields
 *  were. */
export function SelectionRail({
  edges,
  measurements,
  measuring,
  measureError,
  onMeasure,
  name,
  reach,
  tune,
  crowded,
}: Props) {
  const { selLink } = useSelection()

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
          name={name}
          reach={reach}
          tune={tune}
        />
      )
    }
    // A bracket click always carries a valid pair (layout.ts always emits a
    // `linkKey` for a spanned deployment even with no topology edge record
    // yet), but the record itself may not exist -- a pair nothing has ever
    // touched, not even a `measured: false` placeholder. That is still an
    // unmeasured pair, so it gets the exact same rail, not a silent no-op.
    const [a, b] = selLink.split('~') as [string, string]
    return (
      <UnmeasuredLinkRail
        a={a}
        b={b}
        measuring={measuring}
        measureError={measureError}
        onMeasure={onMeasure}
        name={name}
        reach={reach}
        tune={tune}
      />
    )
  }

  return <DefaultRail edges={edges} crowded={crowded} name={name} />
}

// ── Default: the chip list ──────────────────────────────────────────────────

function DefaultRail({
  edges,
  crowded,
  name,
}: {
  edges: TopologyEdge[]
  crowded: boolean
  name: (id: string) => string
}) {
  const { selectLink } = useSelection()
  const measured = edges.filter(edgeMeasured).length

  return (
    <div>
      <p className="unit" style={{ marginBottom: 8 }}>
        Double-click a machine, or click a pair below, for detail. {edges.length} node pairs ·{' '}
        {measured} measured · {edges.length - measured} never measured.
        {crowded
          ? ' Every measured link is on the graph; click a machine to see its unmeasured pairs there too.'
          : ''}
      </p>
      <div className="chips">
        {edges.map((e) => (
          <button key={edgeKey(e.src, e.dst)} onClick={() => selectLink(e.src, e.dst)}>
            {name(e.src)} ↔ {name(e.dst)}
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
  name,
  reach,
  tune,
}: {
  a: string
  b: string
  measuring: string | null
  measureError: { key: string; message: string } | null
  onMeasure: (a: string, b: string) => void
  name: (id: string) => string
  reach: ReachState
  tune: {
    error: { key: string; message: string } | null
    onTune: (a: string, b: string) => void
    decodeBand: number
    bulkBand: number
  }
}) {
  const key = edgeKey(a, b)
  const busy = measuring === key
  return (
    <div>
      <div className="sub" style={{ border: 'none', paddingTop: 0, marginTop: 0 }}>
        link · {name(a)} ↔ {name(b)}
      </div>
      <div className="unit">
        Never measured. No bandwidth figure is shown because none exists — the architecture&apos;s rule
        is that an unmeasured pair carries no numbers at all.
      </div>
      {/* Whether they can talk at all is the cheaper and usually earlier
          question, so it is offered first and is not gated on a measurement
          ever having been taken. */}
      <ReachPanel a={a} b={b} name={name} reach={reach} />
      <TunePanel
        a={a}
        b={b}
        tuning={null}
        measuring={false}
        onTune={tune.onTune}
        error={tune.error?.key === key ? tune.error.message : null}
      />
      {/* "About a minute" stopped being true when the coordinator started
          calibrating a never-calibrated pair off the back of a measurement:
          that adds one two-rank collective per candidate setting. Saying a
          minute and taking five is worse than saying nothing, because the
          reader concludes it hung. */}
      <div className="unit" style={{ marginTop: 10 }}>
        Measuring saturates the link for about a minute — and a pair that has
        never been tuned is calibrated straight afterwards, which takes several
        more.
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
  name,
  reach,
  tune,
}: {
  edge: TopologyEdge
  measurements: LinkMeasurement[]
  measuring: string | null
  measureError: { key: string; message: string } | null
  onMeasure: (a: string, b: string) => void
  name: (id: string) => string
  reach: ReachState
  tune: {
    error: { key: string; message: string } | null
    onTune: (a: string, b: string) => void
    decodeBand: number
    bulkBand: number
  }
}) {
  // Same definition of "measured" as the graph and the chips (edgeMeasured):
  // a measured:true edge with no figure must open the never-measured rail,
  // not a provenance grid asserting thresholds about a number that isn't there.
  if (!edgeMeasured(edge)) {
    return (
      <UnmeasuredLinkRail
        a={edge.src}
        b={edge.dst}
        measuring={measuring}
        measureError={measureError}
        onMeasure={onMeasure}
        name={name}
        reach={reach}
        tune={tune}
      />
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
        link · {name(edge.src)} ↔ {name(edge.dst)}
      </div>
      <div className="chartgrid" style={{ gridTemplateColumns: '1fr 1fr' }}>
        <div>
          <Row label="All-reduce" value={fmtUnit(ar, 1, 'GB/s')} />
          <Row label="Send/recv" value={fmtUnit(sendrecv, 1, 'GB/s')} />
          {/* Absent when no rung measured a COLLECTIVE -- today that is the
              ordinary answer, because only nccl-tests times one and the
              ib_write_bw rung declines rather than offering an ib_write_lat
              for a different operation. Said as "not measured" rather than
              omitted: a missing row reads as a screen that forgot, and a 0
              would read as an instant fabric. */}
          <Row
            label="Latency"
            value={
              edge.latency_us == null
                ? 'not measured'
                : fmtUnit(edge.latency_us, 0, 'µs')
            }
          />
          <Row label="Method" value={method ?? '—'} />
          <TuningRows
            tuning={edge.tuning}
            measuring={edge.measuring === true}
            decodeBand={tune.decodeBand}
            bulkBand={tune.bulkBand}
          />
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
      <ReachPanel a={edge.src} b={edge.dst} name={name} reach={reach} />
      <div className="unit" style={{ marginTop: 8 }}>
        {ar != null && ar >= TP_THRESHOLD
          ? `At or above the ${TP_THRESHOLD} GB/s threshold, so tensor parallel is viable across this pair.`
          : `Below the ${TP_THRESHOLD} GB/s tensor-parallel threshold, which is why the planner chooses pipeline parallel over this pair.`}
      </div>
    </div>
  )
}

/** "coordinator → Rack 2", with each end named the way the plates name it.
 *  `coordinator` is the literal the wire uses for the process answering, not a
 *  node_id, so it is the one end that is never looked up. */
function legDirection(
  leg: { source: string; target: string },
  name: (id: string) => string,
): string {
  const from = leg.source === 'coordinator' ? 'coordinator' : name(leg.source)
  return `${from} → ${name(leg.target)}`
}

/** Can these two actually talk, and from which side?
 *
 *  Separate from the measurement above it because the two answer different
 *  questions at wildly different prices: a measurement saturates the fabric
 *  for about a minute to say how fast, this costs four health checks and says
 *  whether anything gets through at all. Offered on an unmeasured pair too --
 *  a machine that does not answer is a finding you want before you spend a
 *  minute measuring it.
 *
 *  Every leg is listed rather than reduced to one verdict. A one-way failure
 *  is the common real fault (a worker that can dial the coordinator but not be
 *  dialled back), and a merged "not connected" would send an operator to look
 *  at the machine that is demonstrably fine.
 */
function ReachPanel({
  a,
  b,
  name,
  reach,
}: {
  a: string
  b: string
  name: (id: string) => string
  reach: ReachState
}) {
  const key = edgeKey(a, b)
  const busy = reach.checking === key
  const report = reach.report?.key === key ? reach.report.value : null
  const error = !busy && reach.error?.key === key ? reach.error.message : null

  return (
    <div style={{ marginTop: 10 }}>
      <button onClick={() => reach.onCheck(a, b)} disabled={busy}>
        {busy ? 'Checking…' : 'Check connection'}
      </button>
      <span className="unit" style={{ marginLeft: 8 }}>
        A health check each way. Seconds, and safe while this cluster is serving.
      </span>

      {error ? (
        <div className="unit" style={{ marginTop: 8, color: 'var(--fault)' }}>
          {error}
        </div>
      ) : null}

      {report ? (
        <div style={{ marginTop: 8 }}>
          <div className="unit" style={{ color: report.ok ? undefined : 'var(--fault)' }}>
            {report.summary}
          </div>
          {report.legs.map((leg, i) => (
            <div className="row" key={`${leg.source}~${leg.target}~${i}`}>
              <span>{legDirection(leg, name)}</span>
              <span className="mono">
                {/* Three outcomes, not two. A direction nobody could test is
                    not a failure, and a failed one carries no millisecond
                    figure -- 0 ms beside "unreachable" reads as a fast link. */}
                {leg.note
                  ? leg.ok
                    ? '— nothing to dial'
                    : '— not checked'
                  : leg.ok
                    ? `${fmtUnit(leg.ms, 1, 'ms')} · ${leg.url}`
                    : `unreachable · ${leg.url}`}
              </span>
            </div>
          ))}
          {report.legs.map((leg, i) => {
            // An unchecked leg explains itself in muted text; a failure is the
            // thing the eye should land on.
            const unchecked = leg.note != null && !leg.ok
            const detail = unchecked ? leg.note : leg.error
            if (!detail || (leg.ok && leg.note)) return null
            return (
              <div
                className="unit"
                key={`why-${i}`}
                style={{ color: unchecked ? undefined : 'var(--fault)', marginTop: 4 }}
              >
                {legDirection(leg, name)}: {detail}
                {unchecked && leg.error ? ` (${leg.error})` : ''}
              </div>
            )
          })}
          {/* Reaching *something* at an address is not the same as reaching the
              machine you meant. */}
          {report.legs
            .filter((leg) => leg.ok && leg.answered_as && leg.answered_as !== leg.target)
            .map((leg, i) => (
              <div className="unit" key={`id-${i}`} style={{ color: 'var(--fault)', marginTop: 4 }}>
                {leg.url} answered as {leg.answered_as}, not {leg.target}. Two machines may be
                sharing an address.
              </div>
            ))}
          <div className="unit" style={{ marginTop: 4 }}>
            Checked {relativeTime(report.checked_at)}.
          </div>
        </div>
      ) : null}
    </div>
  )
}

/** The Tuning row and, when there is one, the evidence behind the verdict.
 *
 *  The evidence is not decoration. The choice is a trade between two regimes
 *  four decades apart in message size, so the winner alone is an assertion --
 *  seeing that the rejected candidate was FASTER in bulk and slower at decode
 *  is what lets a reader check the rule rather than trust it. */
function TuningRows({
  tuning,
  measuring,
  decodeBand,
  bulkBand,
}: {
  tuning: LinkTuning | null | undefined
  measuring: boolean
  decodeBand: number
  bulkBand: number
}) {
  const state = tuningState(tuning)
  const evidence = tuningEvidence(tuning, decodeBand, bulkBand)
  return (
    <>
      <Row label="Tuning" value={measuring ? 'calibrating…' : tuningLabel(tuning)} />
      {state !== 'uncalibrated' && evidence.length > 1 ? (
        <div className="unit" style={{ marginTop: 4 }}>
          {evidence.map((e) => (
            <div key={e.label} className="mono" style={{ fontSize: '0.85em' }}>
              {e.label}
              {e.decodeUs != null ? ` · ${e.decodeUs.toFixed(1)}µs decode` : ''}
              {e.bulkGbps != null ? ` · ${e.bulkGbps.toFixed(1)} GB/s bulk` : ''}
            </div>
          ))}
        </div>
      ) : null}
    </>
  )
}

/** The button, and the sentence that says what pressing it costs.
 *
 *  `disabled` comes from the SERVER's `measuring`, not from local state: the
 *  request returns in milliseconds and the work runs for minutes, so a local
 *  spinner would clear immediately and invite a second press -- which would
 *  not be refused, because `calibrate` and `measure` share one lock. It would
 *  block, silently, for minutes. */
function TunePanel({
  a,
  b,
  tuning,
  measuring,
  onTune,
  error,
}: {
  a: string
  b: string
  tuning: LinkTuning | null | undefined
  measuring: boolean
  onTune: (a: string, b: string) => void
  error: string | null
}) {
  if (!canTune(tuning, measuring)) return null
  return (
    <div style={{ marginTop: 10 }}>
      <button onClick={() => onTune(a, b)} disabled={measuring}>
        {measuring ? 'Calibrating…' : 'Tune this link'}
      </button>
      <span className="unit" style={{ marginLeft: 8 }}>
        Times one collective per candidate setting. Minutes, and it saturates
        the fabric — not while this cluster is serving.
      </span>
      {error ? (
        <div className="unit" style={{ marginTop: 8, color: 'var(--fault)' }}>{error}</div>
      ) : null}
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
