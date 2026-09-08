// Pure geometry for the machine floor.
//
// No DOM, no React: this module turns cluster state into machine plates, link
// geometry, deployment bands, the request-flow furniture and particle-flight
// polylines. ClusterGraph.tsx is the only thing that ever puts any of it on
// screen, which is what makes this file testable with a plain node script
// (layout.check.mjs) instead of a browser.
//
// Two lineages meet here, deliberately.
//
// The STRUCTURE is a machine floor: one plate per node in /api/topology,
// running or not. It replaced a diagram that iterated DEPLOYMENTS, so a
// machine hosting nothing was never drawn, a machine serving two deployments
// was drawn twice, and the only links on the canvas were those between
// adjacent nodes inside one deployment row. On a live two-node cluster with
// one unmeasured link that rendered one card and zero links.
//
// The LOOK and the FLOW FURNITURE are mockups-next/js/cluster.js: the entry
// box, the served-name plate (which is this file's band), the bandwidth
// bracket and the provider bus, all authored in that
// file's units. Everything below is in AUTHORED units and the viewBox divides
// by GSCALE, so 9 authored px renders at the 12px floor the rest of the panel
// keeps and every glyph, stroke, gap and radius scales together.
//
// Positions are a pure function of the node set plus the persisted
// arrangement -- `order` (which slot a machine is dealt) and `offsets` (how
// far it has been dragged from that slot), both passed in rather than read
// from localStorage so this file stays pure. The same cluster at the same
// viewport with the same arrangement draws identically every time --
// H-ui.md:154 bans a force simulation, and a machine you cannot learn the
// position of is worse than one that is merely plain. Hand-placement is not
// that: it is somebody putting a machine somewhere ON PURPOSE, and it stays
// exactly where they put it until they move it again.
//
// Channel grammar, one meaning each (AUDIT-2026-09-06.md:145):
//
//   width   measured all-reduce bandwidth, and nothing else
//   dash    never measured, and nothing else
//   opacity deprioritized
//   colour  something is WRONG -- healthy is a --rule hairline
//
// so an unmeasured link is a fixed hairline carrying no figure, never a thin
// line implying a small number we do not have.

import type {
  DeploymentDTO,
  Modality,
  RoutingConfig,
  TopologyEdge,
  TopologyNode,
  TopologyRemote,
} from '../../api/types'
import { ENDPOINT_FOR_MODALITY } from '../../api/types'
import { deviceClassLabel, gbytes, planShortFromDegrees, shortGpu } from '../../format'
import { nodeName, nodeSubtitle } from '../../state/names'
import { TERMINAL } from '../models/rows'
import { isArriving, PHASE_LABEL } from './loading'

/** The deployments the floor is a drawing of: the ones still holding a
 *  machine.
 *
 *  `/api/deployments` is a ledger, not a roster -- it keeps every attempt,
 *  and three tries at one model that all failed are three rows with the same
 *  served name and the same node. The band has no vocabulary for "over":
 *  `degraded` and `loading` are the only two conditions it draws, so a failed
 *  row rendered as an ordinary serving band with a 0.0 tok/s readout. Those
 *  three tries therefore drew as three identical runners of one model, each
 *  claiming a machine that is running none of them.
 *
 *  `TERMINAL` is `models/rows.ts`'s, not a second list spelled the same way
 *  here: the Models screen already decides what "over" means for a row, and
 *  the floor disagreeing with it would put two screens at odds about whether
 *  a model is running.
 *
 *  Live duplicates are NOT collapsed. Two deployments of one served name that
 *  are both up are two runners and get two bands, which is what `outFlightKey`
 *  says and what routing does with them -- the ledger was the duplication, not
 *  the cluster. */
export function runners(deployments: readonly DeploymentDTO[]): DeploymentDTO[] {
  return deployments.filter((d) => !TERMINAL.has(d.state))
}

export interface Point {
  x: number
  y: number
}

export type Tier = 'full' | 'compact' | 'chip'
export type FloorKind = 'grid' | 'ring'

/** The viewBox is deliberately smaller than the element, so everything renders
 *  1.333x larger than authored. The graph is authored on a 9px type scale and
 *  the panel's floor is 12px; dividing the coordinate space scales every
 *  glyph, stroke, gap and radius together and not one constant has to move. */
export const GSCALE = 12 / 9

/** Plate size per tier, in authored units. These ARE the mockup's three box
 *  heights: 84 is its selected solo node, 54 its compact solo node, 38 its
 *  remote row. Constant per tier and NEVER divided by the node count -- the
 *  old flow diagram sized boxes as `(TW - (k-1)*GAP)/k`, which goes negative
 *  at five nodes in a row and silently erases them. */
export const CARD: Record<Tier, { w: number; h: number }> = {
  full: { w: 138, h: 84 },
  compact: { w: 102, h: 54 },
  chip: { w: 78, h: 38 },
}

/** Extra plate height reserved for the identity line under a machine's name.
 *  Paid for by the whole floor or by none of it: a row is as tall as its
 *  tallest plate, so letting only the renamed machines grow would leave the
 *  others with the same height and their meters at a different y -- one row of
 *  plates whose innards do not line up reads as a rendering fault.
 *
 *  Full tier only. A compact plate's last row already sits 7 units off its
 *  bottom edge and a chip has no rows at all below the meter, so the line has
 *  nowhere to go there; those tiers carry the identity in the plate's tooltip
 *  and in the node sheet instead. */
export const SUBLINE_H = 12

/** 64 at full tier is the mockup's hard span gap: it is the bandwidth
 *  bracket's channel, and every bracket constant is measured from it. */
const GAP_X: Record<Tier, number> = { full: 64, compact: 48, chip: 36 }
const GAP_Y: Record<Tier, number> = { full: 40, compact: 36, chip: 26 }
const MAX_COLS: Record<Tier, number> = { full: 4, compact: 4, chip: 6 }

export const MARGIN = 16
/** Space above the first row for links that hop over a non-adjacent plate.
 *  Only reserved when one actually will, and `HOP_ROOM` is what those hops are
 *  allowed to spend of it. A hop over any other row has that row's gutter
 *  instead, which is a fact about the floor rather than a reserve. */
export const ARC_HEADROOM = 54

// ── The columns, left to right ───────────────────────────────────────────────
/** Where the entry plate ENDS, and the anchor everything else is measured
 *  from: the elbow gutter, and the x every flight path starts at.
 *
 *  The plate's right edge is fixed and it grows LEFTWARD, which is the whole
 *  reason this constant is a right edge and not a width. The label is a route,
 *  routes get longer, and the previous spelling -- a 132-wide box at x=0 --
 *  could only grow forward into `GUTTER_ENTRY` at 140. Growing left touches
 *  nothing: `offsetX` already exists to re-centre ink that does not start at
 *  zero, and a negative `entry.x` is exactly the case it handles. */
export const ENTRY_R = 132
/** The real route, per endpoint family. Mirrors `ENDPOINT_FOR_MODALITY` in
 *  `control_plane/contracts/modality.py`, and it is a table rather than a
 *  constant for the reason the constant stopped being enough: the floor can
 *  carry a speech deployment and a chat one at the same time, and ONE plate
 *  reading `POST /v1/chat/completions` in front of both is not a simplification
 *  of the routing -- it is a drawing of routing that does not exist. A request
 *  for a TTS model on that endpoint is refused by the gateway with
 *  `wrong_modality`.
 *
 *  Only the families with a band on the floor get a plate; see `entries`. */
export const ENTRY_LABELS: Record<Modality, string> = {
  text: `POST ${ENDPOINT_FOR_MODALITY.text}`,
  embedding: `POST ${ENDPOINT_FOR_MODALITY.embedding}`,
  speech: `POST ${ENDPOINT_FOR_MODALITY.speech}`,
  transcription: `POST ${ENDPOINT_FOR_MODALITY.transcription}`,
}
/** The route every floor with a chat deployment shows, which is nearly all of
 *  them. Derived from the table above rather than typed twice. */
export const ENTRY_LABEL = ENTRY_LABELS.text
export const ENTRY_FONT = 10
export const ENTRY_PAD = 12
/** Computed from the label, never typed beside it, so the two cannot desync.
 *  Plex Mono's advance is 0.6em, so 10px type is 6 units a character.
 *
 *  Per label, because the plate's RIGHT edge is fixed at `ENTRY_R` and it
 *  grows leftward: two plates of different widths still line up on the side
 *  the connectors leave from, and a longer route just reaches further into
 *  the margin `offsetX` already re-centres. */
export function entryWidth(label: string): number {
  return 2 * ENTRY_PAD + Math.ceil(label.length * ENTRY_FONT * 0.6)
}
export const ENTRY_W = entryWidth(ENTRY_LABEL)
export const ENTRY_H = 46
/** Elbow gutter: one entry fans out to N bands through here. */
export const GUTTER_ENTRY = 140
/** Left edge of the machine floor and of everything that spans it. */
export const FLOOR_X = 172

// ── The return side ──────────────────────────────────────────────────────────
/** Exit plate: the response leaving. Mirror of the entry, sized by the same
 *  rule from its own label. */
export const EXIT_LABELS: Record<Modality, string> = {
  text: '200 text/event-stream',
  embedding: '200 application/json',
  // What control_plane/runtimes/tts.py returns by default, and what a
  // provider's TTS returns too: one whole file, not a stream. The pairing
  // with the entry plate above is the point -- a speech band's request and
  // its response are both a different shape from a chat band's, and the
  // drawing said so about neither.
  speech: '200 audio/mpeg',
  transcription: '200 application/json',
}
export const EXIT_LABEL = EXIT_LABELS.text
export const EXIT_FONT = 10
export const EXIT_PAD = 12
export function exitWidth(label: string): number {
  return 2 * EXIT_PAD + Math.ceil(label.length * EXIT_FONT * 0.6)
}
export const EXIT_W = exitWidth(EXIT_LABEL)
export const EXIT_H = 46
/** The provider drop, right of the floor: one vertical run collecting every
 *  allowed band's connection to the bus, then one trunk into the box.
 *
 *  It is on THIS side because this is the side the request is already going.
 *  The rail used to sit at 156, between `GUTTER_ENTRY` and `FLOOR_X`, where it
 *  was collinear with the entry connector's own run at every band's `ny`: the
 *  tap was drawn underneath a line five times its weight and its junction dot
 *  read as a bead threaded onto the entry wire. Leaving from the band's far
 *  corner instead means the connection is drawn where nothing else is, and the
 *  request crosses the band it is served by before it drops. */
export const GUTTER_PROV = 8
/** One vertical run collecting every band's return leg, then one trunk into
 *  the plate. Sixteen clear of the provider drop -- the spacing the left-hand
 *  gutters used between the entry column and the rail. */
export const GUTTER_EXIT = GUTTER_PROV + 16
export const EXIT_GAP = 24
/** Width the floor gives up so the return column has somewhere to be. */
/** The widest plate the table can produce. The floor gives up the same strip
 *  whatever is on it, so the reserve is the maximum and not today's label:
 *  a floor whose only band is speech must not lay its machines out wider than
 *  a floor that also has a chat band, or adding one would move every plate. */
export const EXIT_W_MAX = Math.max(...Object.values(EXIT_LABELS).map(exitWidth))
export const EXIT_RESERVE = GUTTER_EXIT + EXIT_GAP + EXIT_W_MAX

const BAND_H = 36
/** The stepper's own rows, at the pitch a 9px line is read at. */
export const PHASE_ROW_H = 13
/** Where the sweep bar sits inside a loading band, and how thick it is. Below
 *  the two lines every band draws, above the steps only a loading one does. */
export const SWEEP_DY = 38
export const SWEEP_H = 4
/** A band that is still arriving is taller, because it has more to say and
 *  nothing yet to measure: two lines, the indeterminate bar, and one row per
 *  phase. Its own constant rather than an arithmetic expression at the call
 *  site, so the renderer and the floor cannot disagree about how much room the
 *  steps were given -- ClusterGraph draws the first step at SWEEP_DY + SWEEP_H
 *  + 12 and every one after it a PHASE_ROW_H below. */
export const BAND_LOADING_H = SWEEP_DY + SWEEP_H + 12 + 4 * PHASE_ROW_H - 3
const BAND_GAP = 8
const BAND_LEAD_SPACE = 20
const PROVIDER_H = 38
/** One line per provider inside the bus box, under its two fixed lines. */
export const PROVIDER_ROW_H = 13
/** Left inset for the bus box's own two header lines. They are about the bus,
 *  not about any one upstream, so they keep this x and get no mark. */
export const PROVIDER_TEXT_X = 11
/** A provider's own mark, one per row. 11 fits the 13-unit row pitch with two
 *  units of air between tiles. */
export const PROVIDER_LOGO = 11
export const PROVIDER_LOGO_GAP = 5
/** Row text starts after the mark. */
export const PROVIDER_ROW_TEXT_X = PROVIDER_TEXT_X + PROVIDER_LOGO + PROVIDER_LOGO_GAP
/** Tile top, measured from the row's own baseline. A 9px cap sits from
 *  baseline-6.3 to baseline, so -9 brackets it with 2.7 above and 2 below:
 *  optically centred on the words rather than boxed around them. */
export const PROVIDER_LOGO_DY = -9
/** How far below the bus's inbound line its outbound one leaves the same edge.
 *
 *  The drop LANDS on the box's right edge at `busY`; the answer LEAVES it from
 *  the same edge, and one point carrying both would draw a wire that arrives
 *  and departs at once -- the very reading the band's own drop was moved 18
 *  units clear of its return leg to avoid. So it is the same 18, on the same
 *  side, for the same reason. Any row count keeps it inside the box: the box
 *  is at least PROVIDER_H (38) tall and this lands 37 below its top. */
export const PROVIDER_OUT_DY = 18

/** Above this a grid of plates stops being legible and the floor becomes a
 *  ring. The brief contemplates two to four machines. */
export const RING_ABOVE = 12

/** Bandwidth at which tensor parallel becomes viable. A link drawn at full
 *  weight is a link where TP is on the table. */
export const TP_THRESHOLD = 40

/** How far outside its box the renderer draws a plate's hover and selection
 *  ring. The ink box carries it so a fitted drawing cannot clip its own rings. */
const RING_PAD = 3

/** How far apart two plates' tops may be and still be one row.
 *
 *  Once plates can be hand-placed, `row` and `col` describe the SLOT a plate
 *  came from and no longer describe what the drawing looks like -- so every
 *  question the geometry actually asks ("is there a channel between these
 *  two?", "does this band's bar cross a machine that is not its own?") is
 *  answered off x and y instead. The tolerance is for a plate dropped a pixel
 *  off level; the tightest row pitch on any floor is the chip tier's 26-unit
 *  gap plus its plate, so nothing here can merge two rows into one. */
const LEVEL_TOL = 6

/** Bound on a stored hand-placement, in authored units. Not a fence anybody
 *  drags into -- a pointer drag is 1:1 with a pointer that is inside the
 *  window -- but a corrupt or hand-edited value must not be able to fling one
 *  plate so far that the fit shrinks the rest of the floor to nothing. */
export const OFFSET_LIMIT = 4000

export interface ClusterSelection {
  selDep: string | null
  selNode: string | null
  selLink: string | null
}

export interface ClusterLayoutInput {
  nodes: TopologyNode[]
  links: TopologyEdge[]
  deployments: DeploymentDTO[]
  /** What a provider serves, one row per model, from /api/topology. Absent on
   *  a coordinator older than that key, so this is optional and read through
   *  `input.remotes ?? []` -- an old coordinator draws the floor it always
   *  did rather than throwing. */
  remotes?: TopologyRemote[]
  routing: RoutingConfig[]
  selection: ClusterSelection
  /** The graph element's clientWidth in CSS pixels (0 before the first layout
   *  pass, treated as 700). Divided by GSCALE to reach authored units. */
  width: number
  /** The graph element's clientHeight in CSS pixels (0 before the first layout
   *  pass, treated as 420). Only its ratio to `width` is used: the viewBox is
   *  the element's own box, so the drawing can be fitted into all of it. */
  height: number
  /** Persisted drag arrangement: node ids in slot order. null = default.
   *  Passed in, never read from storage here, so this stays a pure function. */
  order: string[] | null
  /** Hand-placement: authored units to displace a plate by, from the slot the
   *  arrangement above gives it. Keyed by node id, absent for a plate nobody
   *  has moved.
   *
   *  A DISPLACEMENT rather than an absolute position, and that is the whole
   *  reason free placement is storable at all. An absolute x/y stops meaning
   *  anything the moment the window resizes or the cluster crosses a density
   *  tier and the plate size changes -- the objection this file used to answer
   *  by refusing free placement outright. An offset carries the plate with its
   *  slot through both, so a floor arranged to match the rack still matches it
   *  on a narrower window. Passed in, never read from storage here. */
  offsets?: Record<string, Point>
}

export interface PlacedCard {
  nodeId: string
  x: number
  y: number
  w: number
  h: number
  /** Index into `slots`. What the keyboard reorder rearranges. */
  slot: number
  /** The slot's row and column. Where this plate CAME FROM, not what the
   *  drawing looks like: a hand-placed plate keeps the slot it was dealt.
   *  Nothing geometric may be decided off these -- see LEVEL_TOL. */
  row: number
  col: number
  /** How far this plate has been hand-placed from its slot, already applied to
   *  x and y above. `{x: 0, y: 0}` for a plate nobody has moved. The renderer
   *  reads it to work out where a drag started from. */
  offset: Point
  selected: boolean
  /** Selection grows a plate in place, as the mockup does, rather than opening
   *  anything over the drawing. A grown plate shows the thermal and hardware
   *  lines; `tier` is the floor's tier, this is what the plate can actually
   *  hold. */
  bodyTier: Tier
}

export interface ClusterBracket {
  x: number
  y: number
  w: number
  h: number
  /** 0..1 against TP_THRESHOLD. Zero for an unmeasured pair -- a blank track,
   *  never a zero-value fill. */
  fraction: number
  /** The invisible taller rect that makes a 7-unit bar clickable. */
  hitY: number
  hitH: number
}

export interface ClusterEdge {
  linkKey: string
  src: string
  dst: string
  /** `bracket` between facing plates -- the mockup's signature bandwidth
   *  element, drawn in the channel between them. `path` for anything that has
   *  to turn a corner, because a bracket cannot span an elbow. */
  kind: 'bracket' | 'path'
  /** Empty for a bracket. */
  d: string
  /** `d` with its corners filleted, and the only one of the three that is
   *  stroked. Empty for a bracket, and identical to `d` on a straight run. */
  dRender: string
  /** The same route as a polyline, corner for corner with `d`. Empty for a
   *  bracket. `motion.ts` interpolates between two of these to move a wire
   *  between two layouts, which cannot be done to a path string. */
  pts: Point[]
  bracket: ClusterBracket | null
  /** Measured: scaled against TP_THRESHOLD. Unmeasured: a fixed hairline,
   *  because width means bandwidth and we do not have one. */
  width: number
  dashed: boolean
  opacity: number
  measured: boolean
  /** "10.2 of 40 GB/s" or "never measured". Never a number that was not
   *  measured, and the threshold is named rather than left implicit. */
  label: string
  labelAt: Point
  showLabel: boolean
  selected: boolean
  aria: string
}

export interface ClusterBand {
  /** Identity, and the renderer's key. A local band's is its deployment id; a
   *  remote band's is `remote:<served name>`, because a name several providers
   *  answer for is ONE band -- the request forks at the provider box, not
   *  before it, and the drop into it is a single run by construction. */
  id: string
  /** null on a remote band. Nothing was deployed, so there is no deployment
   *  to name and no per-deployment metric keyed by one. */
  deploymentId: string | null
  /** Who serves it. A remote band is drawn with the same furniture as a local
   *  one -- that is the point, it is the same served name in the same request
   *  flow -- and differs only where the difference is real: no plan, no
   *  members unless the provider is a machine on this roster, and the words
   *  outline when it is not. */
  kind: 'local' | 'remote'
  /** Routing targets behind a remote band, for the live counters. Empty on a
   *  local band, whose figure is keyed by `deploymentId` instead. */
  targetIds: string[]
  /** Providers standing behind this band. On a LOCAL band these are backups:
   *  LOCAL_FIRST hands them traffic only once every local slot stops
   *  admitting. On a REMOTE band they are the whole of what serves it. */
  providers: string[]
  /** A remote band with no machine of ours under it. Which stack it lands in
   *  and what its sublabel says -- nothing else. It used to also mean "draw
   *  this one dashed"; that vocabulary went with the routing boundary, and
   *  the band's own words ("via openrouter", "off cluster") say it better
   *  than a stroke pattern ever did. */
  offCluster: boolean
  servedName: string
  /** Which endpoint family this name answers on, and therefore which entry
   *  plate it hangs off. `text` for anything the wire did not say, which is
   *  every band drawn before audio existed. */
  modality: Modality
  x: number
  y: number
  w: number
  h: number
  /** Every occupied plate sits on one row, in consecutive columns. When false
   *  the bar is drawn open, with a tick per occupied plate, so a plate inside
   *  the span that is not a member never reads as one. */
  contiguous: boolean
  ticks: number[]
  members: string[]
  /** One polyline per occupied plate, from its own bottom-centre down to this
   *  band. A straight two-point run (`M x y1 L x y2`) while the band sits
   *  where it was packed -- still directly below the machine, exactly as
   *  before hand-placement existed -- and a four-point elbow once `offset`
   *  has moved it out from under its own members (see bandLeads). Empty
   *  when the members are not all on one row -- a lead from the first row
   *  would cross the second. */
  leads: Point[][]
  plan: string
  sublabel: string
  degraded: boolean
  /** Still arriving: PLANNED or LAUNCHING, never DEGRADED (up and serving
   *  badly, which `degraded` already says) and never STOPPING (leaving, not
   *  arriving). A loading band is taller, carries the launch stepper instead
   *  of a throughput readout, and goes back to `BAND_H` the moment the
   *  deployment reports ready.
   *
   *  Only the state is settled here. WHICH step it is on is live -- it moves
   *  on a two-second poll -- so it is read where it is drawn, the same split
   *  `BandThroughput` makes with the 1 Hz frame: the floor must not be laid
   *  out again every time a stepper ticks. */
  loading: boolean
  selected: boolean
  /** Centre y. The entry connector and the provider tap both meet the band
   *  here. */
  ny: number
  /** Hand-placement, both axes: unlike the provider bus this box does not
   *  span the whole floor, so there is real room either side of it to drag
   *  into. Keyed by `id` in the same `offsets` map a machine's own offset
   *  comes from. */
  offset: Point
}

// ── Which provider models belong on the floor ────────────────────────────────
//
// /api/topology reports one remote row per model of every enabled provider,
// so an OpenRouter key with no allowlist is several hundred rows. A band each
// would bury the machines under a catalogue nobody deployed, and "everything
// reachable" is not what the floor is a drawing of.
//
// A row earns a place only when somebody here did something to it:
//
//   backup    a deployment on this floor already serves that name, so the
//             provider is LOCAL_FIRST's overflow valve. It gets no band of its
//             own -- it belongs ON the band it backs, which is what the
//             routing actually does with it.
//   hosted    the provider IS a machine on this roster (the box that enrolled
//             as a GPU-less node and registered as an Ollama provider). The
//             model runs on a plate already drawn, so it sits with the local
//             bands, machine leads and all.
//   named     an alias is set: the served name is not the id the upstream
//             calls it. That is an operator naming a model for their clients.
//   shortlist the providers behind it serve few enough names between them to
//             be a list somebody chose rather than a catalogue we fetched.
//             This is what makes the model allowlist show up here: switching
//             a provider down to three models puts those three on the floor.
//
// Everything else is catalogue: reachable through the proxy, counted on the
// provider bus, and not drawn. The rule reads the topology payload and
// nothing else, so it stays pure and layout.check.mjs can hold it.

/** At or below this many served names, a provider's list is a choice rather
 *  than a catalogue. Four DGX Sparks is the cluster this brief contemplates;
 *  eight remote names is already generous beside it. */
export const SHORTLIST_MAX = 8

export type RemoteRole = 'backup' | 'hosted' | 'named' | 'catalog'

/** One served name, and every provider row behind it. Several providers
 *  answering for one name is the merge, not a collision -- gateway/targets.py
 *  keys the routing table the same way. */
export interface RemoteGroup {
  servedName: string
  role: RemoteRole
  /** Distinct provider ids, sorted. */
  providers: string[]
  /** Every routing target id behind the name, sorted. */
  targetIds: string[]
  /** Roster nodes hosting it, in the order the rows named them. Empty is the
   *  ordinary case: somebody else's hardware. */
  hostIds: string[]
  /** Any provider behind it is up / admitting. One unhealthy provider does not
   *  make the name unservable when another still answers for it. */
  healthy: boolean
  admitting: boolean
  /** The endpoint family every row under this name answers on. One name is
   *  one family by construction -- two models answering different endpoints
   *  are two served names, which is the same assumption gateway/targets.py
   *  makes when it `setdefault`s the modality per name. */
  modality: Modality
}

export function groupRemotes(
  remotes: TopologyRemote[],
  localNames: Set<string>,
): RemoteGroup[] {
  // How many distinct names each provider answers for, which is the whole of
  // the catalogue-versus-shortlist test.
  const offered = new Map<string, Set<string>>()
  const byName = new Map<string, TopologyRemote[]>()
  for (const r of remotes) {
    const names = offered.get(r.provider_id) ?? new Set<string>()
    names.add(r.served_name)
    offered.set(r.provider_id, names)
    const rows = byName.get(r.served_name) ?? []
    rows.push(r)
    byName.set(r.served_name, rows)
  }

  const groups: RemoteGroup[] = []
  for (const [servedName, rows] of byName) {
    const providers = [...new Set(rows.map((r) => r.provider_id))].sort()
    const hostIds = [
      ...new Set(rows.map((r) => r.node_id).filter((id): id is string => id != null)),
    ]
    const shortlisted = providers.some((pid) => (offered.get(pid)?.size ?? 0) <= SHORTLIST_MAX)
    const named = rows.some((r) => r.served_name !== r.upstream_id)
    const role: RemoteRole = localNames.has(servedName)
      ? 'backup'
      : hostIds.length > 0
        ? 'hosted'
        : named || shortlisted
          ? 'named'
          : 'catalog'
    groups.push({
      servedName,
      role,
      providers,
      targetIds: rows.map((r) => r.target_id).sort(),
      hostIds,
      healthy: rows.some((r) => r.state === 'healthy'),
      // `admitting` is null when the coordinator had no routing target to read
      // it off, which is not the same as refusing -- only an explicit false is.
      admitting: rows.some((r) => r.admitting !== false),
      modality: rows.find((r) => r.modality)?.modality ?? 'text',
    })
  }
  groups.sort((a, b) => a.servedName.localeCompare(b.servedName))
  return groups
}

/** "openrouter" or "2 providers". One phrasing wherever a set of providers is
 *  named, so a backup on a local band and the band of a model they serve
 *  outright read as the same relation seen from two sides. */
export function providersLabel(providers: string[]): string {
  if (providers.length === 0) return ''
  return providers.length === 1 ? providers[0]! : `${providers.length} providers`
}

/** Entry box, exit box, and the drops into the provider bus. Flat square-cap
 *  runs, all of them: the butt-cap dash that used to mean "crosses the routing
 *  boundary" went with the boundary, and `dashed` is gone from this type rather
 *  than left behind with no user.
 *
 *  Weight still carries its own meaning, but it no longer carries TWO: a
 *  1-unit hairline used to mean a name merely reachable through the proxy,
 *  drawn from every band on the floor. The allowlist made that false -- a
 *  provider serves nothing but `enabled_models` -- so an unserved name is now
 *  drawn with no connection at all, and 2.5 is the only weight a drop has. */
export interface ClusterConn {
  id: string
  d: string
  /** `d` with its corners filleted -- the flow furniture turns the same
   *  corners the links do and is drawn the same way. */
  dRender: string
  weight: number
  opacity: number
}

/** The provider bus's own "node id" -- there is no NodeProfile or topology
 *  entry behind it, so this is a synthetic key into the same `offsets` map a
 *  real node_id is looked up in, namespaced so it can never collide with one
 *  (a node_id is a hostname; nothing on this cluster is named this). */
export const PROVIDER_NODE_ID = '__provider_bus__'

export interface ClusterProvider {
  x: number
  y: number
  w: number
  h: number
  active: boolean
  label: string
  sublabel: string
  /** One line per provider on the bus, naming the models it actually serves
   *  here and how much more of its catalogue it could reach. The box used to
   *  carry a single aggregate sentence, which could say THAT a provider was
   *  behind the bus and never WHICH provider served WHAT -- the one question
   *  somebody looks at this box to answer. */
  rows: ClusterProviderRow[]
  /** Vertical hand-placement only, in the same units and the same `offsets`
   *  map a machine's own offset comes from (keyed by PROVIDER_NODE_ID). The
   *  box spans the full floor width by construction (`w` is always
   *  `floorRight - FLOOR_X`), so a horizontal component would either widen
   *  it past the floor's own edge or leave the wires that land on that edge
   *  aimed at empty space -- there is no honest place for one to go, so `x`
   *  is always 0. */
  offset: Point
}

export interface ClusterProviderRow {
  providerId: string
  /** "openrouter · gpt-oss-120b, llama-3.1-8b · 312 more reachable". */
  text: string
  /** This provider is carrying traffic for the selected name right now. */
  active: boolean
}

/** An entry or exit plate: a box with a route or a status line on it.
 *
 *  It carries its own label rather than the renderer reaching for a constant,
 *  because there is now more than one of each and which one a plate shows
 *  depends on the bands behind it. */
export interface EndpointPlate {
  x: number
  y: number
  w: number
  h: number
  label: string
  modality: Modality
}

/** Endpoint families in the order their plates are stacked. Chat first
 *  because it is what nearly every floor is mostly made of; the rest in the
 *  order `Modality` itself declares them. Stable, so a floor does not
 *  rearrange itself when a deployment arrives. */
export const MODALITY_ORDER: Modality[] = ['text', 'embedding', 'speech', 'transcription']

/** Bands grouped by endpoint family, families with no band omitted.
 *
 *  Exported for layout.check.mjs: "which bands hang off which plate" is the
 *  whole of the new behaviour, and it is a pure function of the band list. */
export function endpointGroups(bands: ClusterBand[]): [Modality, ClusterBand[]][] {
  const groups: [Modality, ClusterBand[]][] = []
  for (const modality of MODALITY_ORDER) {
    const members = bands.filter((b) => b.modality === modality)
    if (members.length > 0) groups.push([modality, members])
  }
  return groups
}

/** Push overlapping plates apart, in place, keeping their order.
 *
 *  Each plate wants the middle of its own bands, and two families whose bands
 *  interleave want the same middle. Left alone they would be drawn one on top
 *  of the other -- two routes in the same rectangle, which reads as a
 *  rendering fault rather than as two endpoints. The connectors are NOT moved:
 *  a lead still leaves the y its band group was centred on, so the elbow shows
 *  which plate it belongs to even where the plate itself had to shuffle. */
export function separate(plates: EndpointPlate[], h: number): void {
  const gap = 8
  for (let i = 1; i < plates.length; i++) {
    const above = plates[i - 1]!
    const here = plates[i]!
    const floor = above.y + h + gap
    if (here.y < floor) here.y = floor
  }
}

export interface ClusterLayout {
  tier: Tier
  kind: FloorKind
  /** Authored units. The renderer's viewBox, which is the graph element's own
   *  box rather than the drawing's extent: the element fills the floor, and
   *  the renderer's fit transform scales the ink up into all of it. Both are
   *  derived from the same measured ratio, so the viewBox aspect always
   *  equals the element's and preserveAspectRatio never letterboxes. */
  width: number
  height: number
  /** Bounding box of the drawn boxes, in layout coordinates -- i.e. BEFORE the
   *  renderer's translate(offsetX 0). The fit transform frames this. */
  ink: { x: number; y: number; w: number; h: number }
  /** Authored units to slide the whole drawing right so its ink is centred in
   *  the viewBox. The floor centres the machines inside the space left of the
   *  entry box and the bands, which is not the same thing: at two machines on
   *  a wide panel that leaves a third of the panel empty on the right and the
   *  drawing hugging the left edge. Everything is laid out from x = 0 as
   *  before -- this is the last step, applied by the renderer as one translate
   *  so no coordinate in here (paths, slots, drop targeting) has to know about
   *  it. */
  offsetX: number
  card: { w: number; h: number }
  /** SUBLINE_H when some machine on this floor is named something other than
   *  its node_id and the tier has room to say so, 0 otherwise. The renderer
   *  shifts every full-tier plate's rows down by it. */
  subline: number
  cards: PlacedCard[]
  edges: ClusterEdge[]
  bands: ClusterBand[]
  conns: ClusterConn[]
  /** One plate per endpoint family with a band on this floor, in the order
   *  `MODALITY_ORDER` gives. Empty when there is no deployment to enter --
   *  a drawing with no band has nothing to enter and nothing to leave. */
  entries: EndpointPlate[]
  /** The responses leaving, grouped the same way and in the same order, so
   *  the i-th exit is the mirror of the i-th entry. */
  exits: EndpointPlate[]
  provider: ClusterProvider | null
  junctions: { x: number; y: number; r: number; opacity: number }[]
  /** Top-left of every slot, in slot order. Drop targeting reads this. */
  slots: Point[]
  arrangement: string[]
  /** Keyed by `localFlightKey`/`providerFlightKey` -- by the ROUTING TARGET
   *  that serves the flight, not by its position in any list. particles.ts
   *  binds one flight to one target's live request count, so an ordinal key
   *  would silently misattribute the moment a target has no drawable
   *  deployment and the indices shift under it. */
  paths: Record<string, Point[]>
  emptyMessage: string | null
  suppressedPairs: number
}

export function edgeKey(a: string, b: string): string {
  return [a, b].sort().join('~')
}

/** The flight path a LOCAL routing target's requests walk. `targetId` is the
 *  deployment id -- the gateway builds a local target's id from it (see
 *  gateway/targets.py), which is what lets a live per-deployment metric and a
 *  routing target address the same path. */
export function localFlightKey(servedName: string, targetId: string): string {
  return `${servedName}#L:${targetId}`
}

/** The flight path every REMOTE target of a served name shares. There is one
 *  drop into the provider box per served name, not one per upstream model, so
 *  several remote targets legitimately collapse onto this single key and their
 *  live request counts add up on it. */
export function providerFlightKey(servedName: string): string {
  return `${servedName}#P`
}

/** The return leg a served name's OUTPUT TOKENS walk, back out to the client.
 *
 *  Keyed by BAND, not by routing target, and that is not a shortcut: the tok/s
 *  figure this stream is driven by is the band's own, already summed across
 *  every target behind it (`BandThroughput` reads exactly that number). A
 *  per-target return leg would have no measurement to walk it.
 *
 *  A band id is unique within one layout, so two deployments of one served
 *  name are two bands and two streams -- and the `#OUT` suffix cannot collide
 *  with `#L:`/`#P`, which are keyed by served name. */
export function outFlightKey(bandId: string): string {
  return `${bandId}#OUT`
}

/** The one definition of "measured" a link, chip or tally is allowed to use:
 *  the wire's own `measured` flag AND an actual figure to show for it. An edge
 *  that claims `measured: true` but carries no `all_reduce_gbps` has nothing
 *  to draw and must read exactly like one that was never probed -- the rail's
 *  tally uses this too, so the count next to the chips can never disagree with
 *  what the chips themselves say. */
export function edgeMeasured(edge: Pick<TopologyEdge, 'measured' | 'all_reduce_gbps'> | undefined): boolean {
  return edge?.measured === true && edge.all_reduce_gbps != null
}

/** Thickness carries the measurement, scaled against the tensor-parallel
 *  threshold. Authored units, so 4.5 renders at 6. */
export function edgeWidth(gbps: number): number {
  return 1.2 + 4.5 * Math.min(1, gbps / TP_THRESHOLD)
}

export function tierFor(n: number): Tier {
  if (n <= 4) return 'full'
  if (n <= 8) return 'compact'
  return 'chip'
}

/** Plate width for a caption, from the real font metric. IBM Plex Mono's
 *  advance is 0.6em, so 0.6 x 9 = 5.4 exactly; 5.3 approximates 11px Plex
 *  Sans. Both plus 5 units of padding each side. */
export function plateWidth(text: string, mono = true): number {
  return text.length * (mono ? 5.4 : 5.3) + 10
}

/** The gutter a band's throughput readout owns on its right.
 *
 *  Six characters of 13px IBM Plex Mono -- 0.6em advance, so 7.8 a character,
 *  and `svg text.m` sets tabular-nums so every digit is exactly that -- plus
 *  the 11-unit inset the readout is anchored at.
 *
 *  Fixed rather than measured from the figure actually on screen: that number
 *  changes every second off the 1 Hz frame, and a clip that tracked it would
 *  make the served name's last visible character flicker once a second.
 *
 *  Six characters covers every rate up to 9999.9 tok/s, which `fmt(tps, 1)`
 *  renders without separators. A band aggregating more than that grows the
 *  readout leftward past this gutter -- but into space the name has already
 *  been clipped out of, so the two touch rather than overlap. Cramped at five
 *  figures, unreadable at none, which is the right way round.
 */
export const THROUGHPUT_GUTTER = 6 * 7.8 + 11

/** How much of a band's width the served name and sublabel may use.
 *
 *  The throughput readout is right-anchored inside the band while the label
 *  was clipped to the band's *full* width, so a long name ran underneath the
 *  number and neither could be read. `Qwen2.5-0.5B-Instruct` on a one-machine
 *  band is the case that surfaced it: 21 characters of 10px mono is 126 units
 *  and a full-tier plate is 138 wide, so the name reached the readout with
 *  room to spare.
 *
 *  Clipping the name is not a new loss. The band clip already cut it -- a band
 *  is as wide as the machines it occupies, which has nothing to do with how
 *  long its name is -- so the only question was whether it gets cut at the
 *  band's edge, on top of the number, or short of it. Short of it keeps the
 *  measurement legible and costs only the characters that were unreadable
 *  anyway, and the whole string is still in the band's tooltip and its sheet.
 *
 *  Never more than half the band: a chip-tier plate is 78 wide, and the full
 *  gutter there would leave a number with almost nothing beside it to say what
 *  the number is about. A measurement nobody can attribute is worth less than
 *  a name with no measurement.
 */
export function bandLabelWidth(bandW: number): number {
  return Math.max(0, bandW - Math.min(THROUGHPUT_GUTTER, bandW / 2))
}

// ── What a box has to be wide enough to say ──────────────────────────────────
//
// Every box here is sized from the text it draws rather than from a number
// somebody typed, because the two disagree the moment a machine is renamed or
// a model with a long name is deployed: `spark-4d38` and `Qwen2.5-0.5B-Instruct`
// need 197 units between them and a full-tier plate is 138, so the plate drew
// the machine's name through the name of the model it is serving -- and that
// served name is routinely longer than the bar of the one machine serving it.
//
// The drawing is FITTED into the viewBox (see `ink` at the end of
// layoutCluster), so growing a box costs a little scale and never a clipped
// glyph: a floor wider than the panel is drawn smaller, not cut off.

/** Every glyph in this drawing is IBM Plex Mono, whose advance is 0.6em and
 *  whose digits are tabular, so a string's width is arithmetic rather than a
 *  measurement -- no DOM, no font load, and the checker gets the same answer
 *  the browser does. */
export function textWidth(text: string, fontSize: number): number {
  return text.length * fontSize * 0.6
}

/** The inset every box in this drawing sets its text at, and the least air
 *  two texts sharing a line may leave between them before they read as one. */
export const BOX_PAD = 11
export const TEXT_GAP = 8

/** The live rows of a plate, at their widest.
 *
 *  Reserved from a template rather than measured from the figures on screen:
 *  those come off the 1 Hz frame, and a plate whose width tracked them would
 *  breathe once a second and shove the whole floor sideways with it. Keep
 *  these in step with the rows MachinePlate actually draws. */
const POWER_ROW = '9999 W · 999 °C'
const UTIL_ROW = 'GPU 100% · 100% memory'

/** What a machine is running, as the plate says it. Exported because the
 *  width below is measured from this exact string, and a second copy of the
 *  rule in the renderer is how a box stops fitting what it draws. */
export function plateOccupant(servedNames: string[]): string {
  return servedNames.length
    ? `${servedNames[0]}${servedNames.length > 1 ? ` +${servedNames.length - 1}` : ''}`
    : 'free'
}

/** The hardware line along the bottom of a full-tier plate. Same reason. */
export function plateSpec(node: TopologyNode | undefined): string {
  return [
    shortGpu(node?.gpu_name ?? '') || deviceClassLabel(node?.device_class),
    node?.total_memory ? `${gbytes(node.total_memory, 0)} GiB` : '',
  ]
    .filter(Boolean)
    .join(' · ')
}

/** The width one plate needs to draw everything on it without a collision.
 *
 *  The machine's name and its occupant share the top line from opposite
 *  edges, and it is that line rather than the box that failed first: they
 *  overlapped in the middle, so the name of the machine was drawn through the
 *  name of the model it is serving.
 *
 *  `bodyTier` is the largest body the plate can be ASKED to draw, not the one
 *  it is drawing now -- selection promotes a plate a tier in place (see
 *  `bodyTierOf`), and a floor that only fitted its resting tier would collide
 *  the moment somebody clicked. */
export function plateNeed(
  node: TopologyNode,
  occupant: string,
  bodyTier: Tier,
  subline: number,
): number {
  const row = (text: string, size = 9) => BOX_PAD + textWidth(text, size) + BOX_PAD
  const need = [
    BOX_PAD + textWidth(nodeName(node, node.node_id), 9) + TEXT_GAP + textWidth(occupant, 9) + BOX_PAD,
  ]
  if (bodyTier !== 'chip') need.push(row(POWER_ROW))
  if (bodyTier === 'full') {
    need.push(row(UTIL_ROW), row(plateSpec(node)))
    if (subline > 0) need.push(row(nodeSubtitle(node, node.hostname), 8))
  }
  return Math.max(...need)
}

/** The line under a band's served name, exactly as the band draws it. One
 *  spelling, used by the renderer and by the width below. */
export function bandSubline(band: Pick<ClusterBand, 'plan' | 'sublabel' | 'degraded'>): string {
  return [band.plan, band.sublabel, band.degraded ? 'degraded' : ''].filter(Boolean).join(' · ')
}

/** The width a band needs to say what it is AND still show its throughput.
 *
 *  The name sits at `BOX_PAD` from the left and the readout owns
 *  `THROUGHPUT_GUTTER` on the right, so a band this wide has its whole label
 *  inside `bandLabelWidth` and the clip there never bites -- it stays as the
 *  backstop for a font that is not the one we measured. Never narrower than
 *  two gutters, so the half-width floor in `bandLabelWidth` cannot be what
 *  cuts a short name. */
export function bandNeed(servedName: string, subline: string): number {
  return Math.max(
    2 * THROUGHPUT_GUTTER,
    BOX_PAD + Math.max(textWidth(servedName, 10), textWidth(subline, 9)) + THROUGHPUT_GUTTER,
  )
}

/** The extra width a loading band needs for its stepper.
 *
 *  A phase row is a mark, a label and a size, and none of the three is
 *  clipped: the clip that protects the served name is measured against the
 *  throughput readout, which a loading band does not have. Reserved from the
 *  longest label this can ever draw rather than from the one on screen, for
 *  the same reason the plate reserves its live rows from a template -- the
 *  detail column fills in as bytes land, and a bar that widened when it did
 *  would shove the floor sideways mid-download. */
export function bandLoadingNeed(steps: readonly string[], detail: string): number {
  const label = Math.max(0, ...steps.map((t) => textWidth(t, 9)))
  return BOX_PAD + PHASE_MARK_W + label + TEXT_GAP + textWidth(detail, 9) + BOX_PAD
}

/** The mark column: a tick, a dot or a ring, on the 1em box the stylesheet's
 *  `.setup-phase .mark` gives it, plus the gap to the label. */
export const PHASE_MARK_W = 9 + 6

/** The width the bus box needs. Its rows are the only text in the drawing
 *  with no clip and no box of their own to be cut by: too narrow a box and a
 *  provider's model list runs out of the one shape on the floor that means
 *  "not yours" and straight across the return column. */
export function providerNeed(label: string, sublabel: string, rowTexts: string[]): number {
  return Math.max(
    0,
    ...[label, sublabel].map((t) => PROVIDER_TEXT_X + textWidth(t, 9) + BOX_PAD),
    ...rowTexts.map((t) => PROVIDER_ROW_TEXT_X + textWidth(t, 9) + BOX_PAD),
  )
}

const centerOf = (c: PlacedCard): Point => ({ x: c.x + c.w / 2, y: c.y + c.h / 2 })

interface Geometry {
  facing: boolean
  /** A ring's centre-to-centre line. It is the one route that is not made of
   *  axis-aligned runs, so it is neither filleted nor broken at a crossing --
   *  a ring is chords crossing inside a circle, and breaking them would be a
   *  different drawing rather than a clearer one. */
  chord: boolean
  d: string
  /** What is actually stroked: `d` with its corners filleted. See
   *  `roundedPath` -- `d` and `pts` stay the proof and the flight path. */
  dRender: string
  mid: Point
  pts: Point[]
}

/** Waypoints -> an axis-aligned path AND the polyline that matches it, corner
 *  for corner.
 *
 *  The two have to be the same shape. `d` is what the browser strokes and
 *  `pts` is what the particle field walks, so a path that turns a corner the
 *  polyline does not know about flies packets off their own wire. A diagonal
 *  step between two waypoints is therefore squared off HERE, into both at
 *  once, rather than left to each caller to spell twice.
 *
 *  `H`/`V` rather than `L` on purpose: `layout.check.mjs` already reads the
 *  flow connectors as segments to prove none of them crosses a plate, and
 *  spelling links the same way puts them under the same proof. */
function rectilinear(waypoints: Point[]): { d: string; pts: Point[] } {
  const pts: Point[] = []
  const push = (p: Point) => {
    const last = pts[pts.length - 1]
    if (!last || last.x !== p.x || last.y !== p.y) pts.push(p)
  }
  push(waypoints[0]!)
  for (let i = 1; i < waypoints.length; i++) {
    const prev = pts[pts.length - 1]!
    const next = waypoints[i]!
    // Every route below hands over waypoints that already share an axis. This
    // is the fallback for the one case that cannot -- a plate hand-placed a
    // few units off level -- and it squares the step off rather than letting
    // a slant back into the drawing.
    if (prev.x !== next.x && prev.y !== next.y) push({ x: next.x, y: prev.y })
    push(next)
  }
  const parts = [`M${pts[0]!.x} ${pts[0]!.y}`]
  for (let i = 1; i < pts.length; i++) {
    parts.push(pts[i]!.y === pts[i - 1]!.y ? `H${pts[i]!.x}` : `V${pts[i]!.y}`)
  }
  return { d: parts.join(' '), pts }
}

/** How far back from a corner a wire starts turning. Authored units like
 *  everything else here, so it scales with `GSCALE` and nothing has to move. */
export const CORNER_R = 5

/** The hole one wire leaves in another where they cross.
 *
 *  Two wires that cross are drawn as a plus sign, and a reader tracing either
 *  one arrives at a junction the drawing does not distinguish from a corner or
 *  a tee. Breaking one of them is the draftsman's answer and the only one
 *  available here: the floor draws a COMPLETE mesh, and every graph on five or
 *  more meshed machines is non-planar, so the crossings cannot be arranged
 *  away by any placement -- they can only be drawn well.
 *
 *  Which wire gives way is not a new channel. It is the one already there:
 *  bandwidth. The faster link is the one somebody is trying to follow, so it
 *  stays whole and the slower one is broken; unmeasured always gives way to
 *  measured, and two links with nothing between them break on `linkKey` so the
 *  drawing stays a pure function of its input. */
export const CROSS_GAP = 4

/** The same route, drawn with its corners filleted.
 *
 *  This is a THIRD spelling of one polyline and the only one that is ever
 *  stroked. `d` stays the proof -- `layout.check.mjs` reads it to show every
 *  run is axis-aligned and that both ends land on their own plate -- and `pts`
 *  stays what `particles.ts` walks. A fillet is a rendering treatment, not a
 *  route, and giving it its own field is what lets those two go on being
 *  exactly as strict as they were.
 *
 *  The radius is decided per corner and never globally. A riser six units long
 *  between two lanes gets a three-unit fillet, because a fillet longer than
 *  half its own run would reach past the next corner -- and on the run into a
 *  plate, past the edge the wire is supposed to land on. Under a unit it is
 *  not drawn at all: these coordinates go into a path string, and a corner
 *  rounded by a third of a unit costs seventeen digits to say nothing. */
export function roundedPath(
  route: readonly Point[],
  r: number,
  breaks: readonly Point[] = [],
): string {
  // A vertex its two neighbours are in line with is not a corner, and a fillet
  // drawn about one is a curve from a straight line to the same straight line.
  // `rectilinear` leaves them behind -- a connector whose band sits level with
  // its entry plate turns into the gutter and straight back out again -- and
  // rounding is where they start costing a command each.
  const pts = route.filter((p, i) => {
    const a = route[i - 1]
    const b = route[i + 1]
    if (!a || !b) return true
    return !((a.y === p.y && p.y === b.y) || (a.x === p.x && p.x === b.x))
  })
  if (pts.length < 2) return ''
  const at = (i: number) => pts[i]!
  // `k` along the run, and ONLY along it. Rounding both coordinates -- for the
  // reason `anchorOn` rounds, that a fillet placed at a fraction of a unit is a
  // longer string and the same picture -- moved the across-the-run coordinate
  // too, which walks the wire a third of a unit off the route it is rounding
  // and takes every corner after it along. A gutter dealt at 618.7 stays at
  // 618.7; what gets rounded is how far back from the corner the turn starts.
  const round = (v: number) => Math.round(v)
  const toward = (from: Point, to: Point, k: number): Point => {
    if (from.y === to.y && from.x !== to.x) {
      return { x: round(from.x + Math.sign(to.x - from.x) * k), y: from.y }
    }
    if (from.x === to.x && from.y !== to.y) {
      return { x: from.x, y: round(from.y + Math.sign(to.y - from.y) * k) }
    }
    return { x: from.x, y: from.y }
  }
  const parts = [`M${at(0).x} ${at(0).y}`]
  let cur = at(0)
  /** One straight run, minus the holes any crossing wire punched in it.
   *
   *  Every hole is opened HERE rather than by editing the finished string,
   *  because this is the only place that knows a run is straight: `run` is
   *  called between one fillet's exit and the next one's entry, so a gap can
   *  never eat a corner. A crossing too close to either end for the whole gap
   *  to fit is left undrawn -- a wire that stops a unit short of its own
   *  corner reads as a routing fault, which is worse than the crossing. */
  const run = (to: Point) => {
    if (to.x === cur.x && to.y === cur.y) return
    const horiz = to.y === cur.y
    const half = CROSS_GAP / 2
    const from = horiz ? cur.x : cur.y
    const end = horiz ? to.x : to.y
    const dir = end > from ? 1 : -1
    const along = (p: Point) => (horiz ? p.x : p.y)
    const on = (p: Point) => (horiz ? p.y === cur.y : p.x === cur.x)
    const cuts = breaks
      .filter((b) => on(b))
      .map(along)
      .filter((v) => (v - from) * dir > half + 0.5 && (end - v) * dir > half + 0.5)
      .sort((p, q) => (p - q) * dir)
    for (const v of cuts) {
      const before = horiz ? { x: v - dir * half, y: cur.y } : { x: cur.x, y: v - dir * half }
      const after = horiz ? { x: v + dir * half, y: cur.y } : { x: cur.x, y: v + dir * half }
      parts.push(horiz ? `H${before.x}` : `V${before.y}`)
      parts.push(`M${after.x} ${after.y}`)
      cur = after
    }
    parts.push(horiz ? `H${to.x}` : `V${to.y}`)
    cur = to
  }
  for (let i = 1; i < pts.length - 1; i++) {
    const prev = at(i - 1)
    const corner = at(i)
    const next = at(i + 1)
    const back = Math.abs(corner.x - prev.x) + Math.abs(corner.y - prev.y)
    const on = Math.abs(next.x - corner.x) + Math.abs(next.y - corner.y)
    const k = Math.min(r, back / 2, on / 2)
    if (k < 1) {
      run(corner)
      continue
    }
    const enter = toward(corner, prev, k)
    const leave = toward(corner, next, k)
    run(enter)
    parts.push(`Q${corner.x} ${corner.y} ${leave.x} ${leave.y}`)
    cur = leave
  }
  run(at(pts.length - 1))
  return parts.join(' ')
}

/** A piece of flow furniture, from its waypoints rather than from a path
 *  string spelled by hand. The connectors used to be authored as `d` directly
 *  and the return leg then repeated itself as a points array for the flights
 *  to walk; going through `rectilinear` means the wire, the proof and the
 *  flight path are one description of one route. */
/** A drawn wire, kept beside the polyline it was drawn from and the number
 *  that decides which of two crossing wires gives way. `rank` is the measured
 *  all-reduce figure, or -1 for a pair nobody has probed. */
interface RoutedEdge {
  edge: ClusterEdge
  pts: Point[]
  rank: number
}

/** Where two axis-aligned runs cross, if they properly cross at all.
 *
 *  Strictly interior to both, by more than the gap that would be punched
 *  there: a wire that merely ENDS on another one is a tee, and a tee is not
 *  ambiguous -- breaking it would invent a crossing the drawing does not
 *  have. Links leaving the same plate are the common case, and `fanOff`
 *  already pulls their risers apart. */
function runsCross(
  p: [Point, Point],
  q: [Point, Point],
): Point | null {
  const horiz = (r: [Point, Point]) => r[0].y === r[1].y
  const vert = (r: [Point, Point]) => r[0].x === r[1].x
  const [h, v] = horiz(p) && vert(q) ? [p, q] : vert(p) && horiz(q) ? [q, p] : [null, null]
  if (!h || !v) return null
  const t = CROSS_GAP / 2 + 0.5
  const x = v[0].x
  const y = h[0].y
  const inside = (a: number, lo: number, hi: number) =>
    a > Math.min(lo, hi) + t && a < Math.max(lo, hi) - t
  if (!inside(x, h[0].x, h[1].x) || !inside(y, v[0].y, v[1].y)) return null
  return { x, y }
}

/** Break the slower of every crossing pair, and redraw only the wires that
 *  were broken.
 *
 *  Mutates `dRender` and nothing else: `d` stays the squared-off proof every
 *  assertion in `layout.check.mjs` reads, and `pts` stays the polyline
 *  `particles.ts` walks, so a block still flies down a wire that now has a
 *  hole in it -- which is right. The hole is about reading the drawing, not
 *  about where the bytes go. */
function breakAtCrossings(routed: readonly RoutedEdge[]): void {
  const runs = (r: RoutedEdge): [Point, Point][] =>
    r.pts.slice(1).map((p, i): [Point, Point] => [r.pts[i]!, p])
  const holes = new Map<string, Point[]>()
  const all = routed.map((r) => ({ r, runs: runs(r) }))
  for (let i = 0; i < all.length; i++) {
    for (let j = i + 1; j < all.length; j++) {
      const a = all[i]!
      const b = all[j]!
      // Higher bandwidth stays whole. Equal -- and two unmeasured links are
      // equal -- breaks on the key, so the same floor draws the same holes.
      const gives =
        a.r.rank !== b.r.rank
          ? a.r.rank < b.r.rank
            ? a
            : b
          : a.r.edge.linkKey > b.r.edge.linkKey
            ? a
            : b
      for (const p of a.runs) {
        for (const q of b.runs) {
          const at = runsCross(p, q)
          if (!at) continue
          const list = holes.get(gives.r.edge.linkKey) ?? []
          list.push(at)
          holes.set(gives.r.edge.linkKey, list)
        }
      }
    }
  }
  for (const r of routed) {
    const cut = holes.get(r.edge.linkKey)
    if (cut?.length) r.edge.dRender = roundedPath(r.pts, CORNER_R, cut)
  }
}

function connFrom(id: string, waypoints: Point[], weight: number, opacity: number): ClusterConn {
  // The fillet is taken off `rectilinear`'s polyline and not off the waypoints,
  // for the same reason the links do it: a waypoint pair that is not already
  // axis-aligned gains a corner there, and a wire rounded before that corner
  // exists is not a rounding of the route that is drawn.
  const { d, pts } = rectilinear(waypoints)
  return { id, d, dRender: roundedPath(pts, CORNER_R), weight, opacity }
}

/** Does an axis-aligned run pass through a plate? One unit of tolerance, so a
 *  run that merely lands on a plate's edge does not count as crossing it --
 *  the same rule `segmentHitsRect` applies to connectors in the verifier. */
function runHitsCard(a: Point, b: Point, c: PlacedCard): boolean {
  const t = 1
  return (
    Math.max(a.x, b.x) > c.x + t &&
    Math.min(a.x, b.x) < c.x + c.w - t &&
    Math.max(a.y, b.y) > c.y + t &&
    Math.min(a.y, b.y) < c.y + c.h - t
  )
}

/** The rule that made the old links arcs, asked of a candidate route instead
 *  of assumed: does it touch a machine it is not a link between? */
function routeIsClear(pts: Point[], a: PlacedCard, b: PlacedCard, floor: PlacedCard[]): boolean {
  for (let i = 1; i < pts.length; i++) {
    for (const c of floor) {
      if (c === a || c === b) continue
      if (runHitsCard(pts[i - 1]!, pts[i]!, c)) return false
    }
  }
  return true
}

/** Facing plates get a straight segment between their near edges -- that
 *  channel is where the bracket goes. Anything else turns corners, so a link
 *  never runs underneath a machine it does not touch. */
/** Two plates share a row in the DRAWING, whatever slots they were dealt. */
function level(a: PlacedCard, b: PlacedCard): boolean {
  return Math.abs(a.y - b.y) <= LEVEL_TOL
}

/** Level, with a clear channel between them: no third plate standing in the
 *  gap. On the default grid this answers exactly what `col` adjacency used to
 *  -- an intervening column always holds a plate -- and it goes on answering
 *  it once somebody has moved the plates by hand, which `col` cannot. */
function sideBySide(a: PlacedCard, b: PlacedCard, floor: PlacedCard[]): boolean {
  if (!level(a, b)) return false
  const [l, r] = a.x <= b.x ? [a, b] : [b, a]
  return !floor.some(
    (c) => c !== a && c !== b && level(c, a) && c.x + c.w > l.x + l.w && c.x < r.x,
  )
}

/** The same question down the other axis: column-aligned, nothing in between.
 *  Replaces the `col` equality plus adjacent-`row` test, for the same reason. */
function stacked(a: PlacedCard, b: PlacedCard, floor: PlacedCard[]): boolean {
  if (Math.abs(a.x + a.w / 2 - (b.x + b.w / 2)) > LEVEL_TOL) return false
  const [t, bm] = a.y <= b.y ? [a, b] : [b, a]
  return !floor.some(
    (c) =>
      c !== a &&
      c !== b &&
      c.y + c.h > t.y + t.h &&
      c.y < bm.y &&
      c.x + c.w > Math.max(t.x, bm.x) &&
      c.x < Math.min(t.x + t.w, bm.x + bm.w),
  )
}

// -- Lanes --------------------------------------------------------------------
//
// Squaring the links off is what turned "where does this link go?" into a
// question about the whole floor rather than about two plates.
//
// A quadratic only ever had to know its own endpoints. Two curves across the
// same row gutter diverge, and a reader can follow either one. Two RUNS across
// it at the same height do not diverge: wherever they overlap they are one
// line, and one of the two links is simply not in the drawing any more. So
// every run that has to cross a gap -- the headroom above a row, the gutter
// between two rows, the channel beside a column -- is dealt a lane in it.
//
// The dealing is the textbook interval colouring: sort the runs by where each
// one starts and give it the lowest lane whose previous run has already ended.
// It uses the fewest lanes any assignment could, which is the whole point --
// the gutter is 40 units on a full floor and every lane spent is one the rest
// have to share.

/** How far apart lanes would like to be, before the gap has its say. */
const LANE_IDEAL = 13

/** Lane `i` of `of`, and both are needed: a lane is placed by its share of the
 *  gap, so a sole hop sits close over its row rather than out at the ceiling. */
interface Lane {
  i: number
  of: number
}

/** One run's claim on a gap: which gap, the stretch of it this run covers, and
 *  how to turn the lane it is dealt into a coordinate. */
interface Claim {
  group: string
  lo: number
  hi: number
  place: (lane: Lane) => number
}

function assignLanes(claims: Map<string, Claim>): Map<string, Lane> {
  const byGroup = new Map<string, { key: string; lo: number; hi: number }[]>()
  for (const [key, c] of claims) {
    const bucket = byGroup.get(c.group) ?? []
    bucket.push({ key, lo: c.lo, hi: c.hi })
    byGroup.set(c.group, bucket)
  }
  const out = new Map<string, Lane>()
  for (const bucket of byGroup.values()) {
    // Ties broken on the key so the drawing stays a pure function of its
    // input: a lane that depended on Map order would move when the gateway
    // happened to list the same links in a different order.
    bucket.sort((p, q) => p.lo - q.lo || p.hi - q.hi || (p.key < q.key ? -1 : 1))
    const ends: number[] = []
    const lane = new Map<string, number>()
    for (const run of bucket) {
      let i = ends.findIndex((end) => end <= run.lo + 0.5)
      if (i < 0) i = ends.push(-Infinity) - 1
      ends[i] = run.hi
      lane.set(run.key, i)
    }
    for (const [key, i] of lane) out.set(key, { i, of: ends.length })
  }
  return out
}

/** Lane `i` of `of` placed inside a gap that runs from `at` to `at + span`,
 *  with both ends kept clear. Evenly spaced, so the gutter between two rows
 *  reads as a bus of parallel runs rather than a pile against one edge. */
function inGap(lane: Lane, at: number, span: number): number {
  return at + (span * (lane.i + 1)) / (lane.of + 1)
}

/** The same, for a gap open at one end -- the headroom above a row, which
 *  nothing bounds except the plates on the row above and the reserve the floor
 *  made. `LANE_IDEAL` is what keeps a floor with one hop on it from drawing
 *  that hop up at the ceiling. */
function overRow(lane: Lane, room: number): number {
  const spread = Math.min(room, LANE_IDEAL * lane.of)
  return (spread * (lane.i + 1)) / lane.of
}

/** How far off a plate's middle the risers of two links in the same gap are
 *  pulled apart, and how far off it any of them may ever get.
 *
 *  A lane keeps two links from being drawn as one line ACROSS a gap. It does
 *  nothing for the short runs into the plates at either end, and on a 2x2
 *  floor those are the ambiguous part: both diagonals rise out of the same
 *  column, so a reader is left with one vertical line and no way to tell which
 *  of the two plates on it the crossing run belongs to.
 *
 *  So a link's risers step off centre by its lane, both ends by the same
 *  amount, which keeps the run across the gap exactly where its lane put it.
 *  The offsets skip zero deliberately: the middle of a plate's edge belongs to
 *  the link that goes straight down from it to the plate underneath, and that
 *  one has no lane to be dealt. */
const LANE_FAN = 9
const FAN_MAX = 18

function fanOff(lane: Lane): number {
  const k = lane.i - (lane.of - 1) / 2
  // Two units is the floor, not a preference. A gutter carrying more runs than
  // it has room for shrinks the step, and a step under a unit rounds two
  // risers onto the same line -- which is the thing the fan is here to stop.
  const step = Math.max(2, Math.min(LANE_FAN, FAN_MAX / Math.max(0.5, lane.of / 2)))
  return (k >= 0 ? k + 0.5 : k - 0.5) * step
}

/** A riser's attachment, kept inside the plate's own edge: an offset wide
 *  enough to matter on a full plate would hang off a chip one. Rounded,
 *  because these coordinates are written into a path string and a fan of
 *  sevenths would put seventeen digits of nothing into every one of them. */
function anchorOn(at: number, size: number, off: number): number {
  const half = Math.max(0, size / 2 - 10)
  return Math.round(at + size / 2 + Math.max(-half, Math.min(half, off)))
}

/** Merged, sorted intervals of an axis the plates take up: `rowBands` down the
 *  y axis, `occupied` across x for one horizontal slice. What is left between
 *  them is where a run may go. */
function merge(spans: [number, number][]): [number, number][] {
  const out: [number, number][] = []
  for (const s of [...spans].sort((p, q) => p[0] - q[0])) {
    const last = out[out.length - 1]
    if (last && s[0] <= last[1]) last[1] = Math.max(last[1], s[1])
    else out.push([s[0], s[1]])
  }
  return out
}

function rowBands(floor: PlacedCard[]): [number, number][] {
  return merge(floor.map((c): [number, number] => [c.y, c.y + c.h]))
}

function occupied(floor: PlacedCard[], lo: number, hi: number): [number, number][] {
  return merge(
    floor.filter((c) => c.y + c.h > lo && c.y < hi).map((c): [number, number] => [c.x, c.x + c.w]),
  )
}

/** Clearance a run keeps from the plates either side of the gap it is in, and
 *  so also the narrowest gap worth calling one. */
const CHANNEL_CLEAR = 8

/** The vertical channels a run could go down between `lo` and `hi`, left to
 *  right. The two outermost are open-ended, so they are given a channel's
 *  width and no more; the caller picks by what the route through each costs. */
function channels(floor: PlacedCard[], lo: number, hi: number): [number, number][] {
  const spans = occupied(floor, lo, hi)
  if (spans.length === 0) return []
  const wide = CHANNEL_CLEAR * 4
  const out: [number, number][] = [[spans[0]![0] - wide, spans[0]![0]]]
  for (let i = 1; i < spans.length; i++) {
    if (spans[i]![0] - spans[i - 1]![1] >= CHANNEL_CLEAR * 2) out.push([spans[i - 1]![1], spans[i]![0]])
  }
  out.push([spans[spans.length - 1]![1], spans[spans.length - 1]![1] + wide])
  return out
}

/** Headroom a hop may claim when nothing is above it. `ARC_HEADROOM` is what
 *  the floor reserves above its first row, and a hop's bandwidth caption backs
 *  onto its own lane, so the caption's 9 units of ascent have to fit inside
 *  that reserve too. */
const HOP_ROOM = ARC_HEADROOM - 10

/** A pair's route, before the lanes it needs have been dealt. `build` turns
 *  the dealt coordinates -- one per claim, in order -- into the waypoints. */
interface Plan {
  facing: boolean
  /** The ring floor's centre-to-centre line: the one route that is not made
   *  of axis-aligned runs, and so the one that is not `rectilinear`. */
  chord: boolean
  claims: Claim[]
  /** The route, from the coordinate each claim was dealt and the lane it was
   *  dealt in -- the coordinate places the run across the gap, the lane places
   *  the risers into the plates at either end. */
  build: (at: number[], lanes: Lane[]) => Point[]
}

const flat = (facing: boolean, pts: Point[]): Plan => ({
  facing,
  chord: false,
  claims: [],
  build: () => pts,
})

function planRoute(a: PlacedCard, b: PlacedCard, kind: FloorKind, floor: PlacedCard[]): Plan {
  if (kind === 'ring') {
    // A ring has no rows to run between and no gutters to run in. The chord
    // between two centres IS the direct line; squaring it off would draw two
    // sides of a box across the inside of a circle.
    return { facing: false, chord: true, claims: [], build: () => [centerOf(a), centerOf(b)] }
  }

  // Every run that is not between two plates directly facing each other goes
  // through a GUTTER: a stripe of the floor that no plate reaches into at any
  // x. A run inside one is clear by construction, so clearance never has to be
  // re-checked once a lane has been dealt in it -- and a hop over a row and a
  // drop past it share the gutter between those rows, so they have to share
  // its lanes too. Two links in one gap dealt from two decks is two links
  // drawn on top of each other.
  const bands = rowBands(floor)
  const bandOf = (c: PlacedCard) => bands.findIndex((s) => c.y >= s[0] && c.y + c.h <= s[1])
  const gutter = (i: number): [number, number] => [bands[i]![1], bands[i + 1]![0]]
  // Claimed with `FAN_MAX` of slack at each end, because the run is dealt its
  // lane before it knows how far off centre the fan will push its risers, and
  // a claim measured to the plate centres would let two runs that were told to
  // abut there overlap by however far the fan then moved them.
  const across = (g: [number, number], lo: number, hi: number): Claim => ({
    group: `gutter@${Math.round(g[0])}`,
    lo: Math.min(lo, hi) - FAN_MAX,
    hi: Math.max(lo, hi) + FAN_MAX,
    place: (lane) => Math.round(inGap(lane, g[0], g[1] - g[0])),
  })

  if (level(a, b)) {
    const [l, r] = a.x <= b.x ? [a, b] : [b, a]
    if (sideBySide(a, b, floor)) {
      const y = l.y + l.h / 2
      return flat(true, [
        { x: l.x + l.w, y },
        { x: r.x, y },
      ])
    }

    // Over the top: up out of one plate, across above the row, down into the
    // other. Three runs and two corners, where this was a quadratic.
    const p0 = { x: l.x + l.w / 2, y: l.y }
    const p1 = { x: r.x + r.w / 2, y: r.y }
    const top = Math.min(p0.y, p1.y)
    const bi = bandOf(l)
    // A hop over a middle row has the gutter above that row and nothing more
    // -- 26 units on a chip floor -- and one drawn out at the reserve instead
    // would run straight through the plates on top of it, which is the one
    // thing a hop exists to avoid. Only the first row hops into open sky, and
    // `ARC_HEADROOM` is the reserve the floor made for exactly that.
    const claim: Claim =
      bi > 0 && gutter(bi - 1)[1] > gutter(bi - 1)[0]
        ? across(gutter(bi - 1), p0.x, p1.x)
        : {
            group: `over@${Math.round(top)}`,
            lo: p0.x - FAN_MAX,
            hi: p1.x + FAN_MAX,
            place: (lane) => Math.round(top - overRow(lane, HOP_ROOM)),
          }
    return {
      facing: false,
      chord: false,
      claims: [claim],
      build: ([y], [lane]) => {
        const off = fanOff(lane!)
        const x0 = anchorOn(l.x, l.w, off)
        const x1 = anchorOn(r.x, r.w, off)
        return [
          { x: x0, y: p0.y },
          { x: x0, y: y! },
          { x: x1, y: y! },
          { x: x1, y: p1.y },
        ]
      },
    }
  }

  const [t, bm] = a.y <= b.y ? [a, b] : [b, a]
  if (stacked(t, bm, floor)) {
    // One vertical, not a slant of a unit or two: `stacked` tolerates
    // LEVEL_TOL of centre drift so a hand-placed plate still counts as under
    // its neighbour, and splitting that drift between the two ends draws the
    // line the arrangement means rather than the one the pixels happen to say.
    const x = Math.round((t.x + t.w / 2 + bm.x + bm.w / 2) / 2)
    return flat(true, [
      { x, y: t.y + t.h },
      { x, y: bm.y },
    ])
  }

  const bi = bandOf(t)
  const bj = bandOf(bm)

  if (bi >= 0 && bj > bi && gutter(bi)[1] - gutter(bi)[0] >= CHANNEL_CLEAR) {
    const p0 = { x: t.x + t.w / 2, y: t.y + t.h }
    const p1 = { x: bm.x + bm.w / 2, y: bm.y }
    const g1 = gutter(bi)

    // Neighbouring rows: down into the gutter, across it, down again.
    if (bj === bi + 1) {
      return {
        facing: false,
        chord: false,
        claims: [across(g1, p0.x, p1.x)],
        build: ([y], [lane]) => {
          const off = fanOff(lane!)
          const x0 = anchorOn(t.x, t.w, off)
          const x1 = anchorOn(bm.x, bm.w, off)
          return [
            { x: x0, y: p0.y },
            { x: x0, y: y! },
            { x: x1, y: y! },
            { x: x1, y: p1.y },
          ]
        },
      }
    }

    // Rows further apart than that: there is a row in between, so the run
    // takes the stairs. Down into the first gutter, along it to a column
    // channel, down the channel past everything in the way, along the gutter
    // above the far plate and down into it -- four corners, and the only
    // shape that gets between two rows with a third one between them without
    // being drawn across it.
    const g2 = gutter(bj - 1)
    const options = channels(floor, g1[1], g2[0])
    let gap: [number, number] | null = null
    let cost = Infinity
    for (const c of options) {
      const x = (c[0] + c[1]) / 2
      const len = Math.abs(p0.x - x) + Math.abs(x - p1.x)
      if (len < cost) {
        cost = len
        gap = c
      }
    }
    if (gap) {
      const g = gap
      return {
        facing: false,
        chord: false,
        // Claimed against the whole width of the channel, not against the
        // middle of it. Where in the channel the run actually goes is another
        // lane's answer and it is not known yet, so a claim measured to the
        // middle would let two stairs that turn into the same channel from
        // opposite sides share a gutter lane and then overlap in it by
        // however far apart their channel lanes turned out to be.
        claims: [
          across(g1, p0.x, p0.x < g[1] ? g[1] : g[0]),
          {
            group: `channel@${Math.round(g[0])}`,
            lo: g1[0],
            hi: g2[1],
            place: (lane) => Math.round(inGap(lane, g[0], g[1] - g[0])),
          },
          across(g2, p1.x, p1.x < g[1] ? g[1] : g[0]),
        ],
        build: ([y1, x, y2], [lane1, , lane2]) => {
          const x0 = anchorOn(t.x, t.w, fanOff(lane1!))
          const x1 = anchorOn(bm.x, bm.w, fanOff(lane2!))
          return [
            { x: x0, y: p0.y },
            { x: x0, y: y1! },
            { x: x!, y: y1! },
            { x: x!, y: y2! },
            { x: x1, y: y2! },
            { x: x1, y: p1.y },
          ]
        },
      }
    }
  }

  // No usable gutter at all: plates hand-dragged until their rows overlap.
  // Leave from the SIDE and go down a channel beside them. Every channel is
  // tried and the shortest one that touches nothing wins; the old spelling
  // bulged a curve sideways by 0.6 plate-widths, which cleared the obstruction
  // only because every plate on a floor happens to be the same width, and
  // cleared it by passing through the gap rather than by knowing it was there.
  const y0 = t.y + t.h / 2
  const y1 = bm.y + bm.h / 2
  const ends = (x: number, off = 0) => [
    { x: x > t.x + t.w / 2 ? t.x + t.w : t.x, y: anchorOn(t.y, t.h, off) },
    { x: x > bm.x + bm.w / 2 ? bm.x + bm.w : bm.x, y: anchorOn(bm.y, bm.h, off) },
  ]
  const options = channels(floor, y0, y1)
  let gap: [number, number] | null = null
  let cost = Infinity
  for (const c of options) {
    const x = (c[0] + c[1]) / 2
    const [s0, s1] = ends(x)
    const run = rectilinear([s0!, { x, y: y0 }, { x, y: y1 }, s1!])
    if (!routeIsClear(run.pts, t, bm, floor)) continue
    const len = Math.abs(x - s0!.x) + Math.abs(y1 - y0) + Math.abs(s1!.x - x)
    if (len < cost) {
      cost = len
      gap = c
    }
  }
  // Nothing is clear, on a floor somebody has piled into one corner. Take the
  // channel nearest the pair and draw the link anyway: a link drawn across a
  // plate is wrong, and a link not drawn at all is a machine that reads as
  // unwired, which is worse.
  if (!gap) {
    const mid = (t.x + t.w / 2 + bm.x + bm.w / 2) / 2
    for (const c of options) {
      const d = Math.abs((c[0] + c[1]) / 2 - mid)
      if (d < cost) {
        cost = d
        gap = c
      }
    }
  }
  const g = gap ?? [t.x + t.w, t.x + t.w + CHANNEL_CLEAR * 2]
  return {
    facing: false,
    chord: false,
    claims: [
      {
        group: `beside@${Math.round(g[0])}`,
        lo: Math.min(y0, y1) - FAN_MAX,
        hi: Math.max(y0, y1) + FAN_MAX,
        place: (lane) => Math.round(inGap(lane, g[0], g[1] - g[0])),
      },
    ],
    build: ([x], [lane]) => {
      const [s0, s1] = ends(x!, fanOff(lane!))
      return [s0!, { x: x!, y: s0!.y }, { x: x!, y: s1!.y }, s1!]
    },
  }
}

/** The caption sits on the link's longest straight run, which is the only part
 *  of it with room for a caption. On a hop or a drop that is the run across
 *  the gap; on a plate-to-plate channel it is the whole link. */
function longestRun(pts: Point[]): Point {
  let best = { x: pts[0]!.x, y: pts[0]!.y }
  let len = -1
  for (let i = 1; i < pts.length; i++) {
    const a = pts[i - 1]!
    const b = pts[i]!
    const l = Math.abs(b.x - a.x) + Math.abs(b.y - a.y)
    if (l > len) {
      len = l
      best = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 }
    }
  }
  return best
}

/** Every link on the floor routed at once, then handed back one pair at a
 *  time. Both callers -- the links themselves and the flight paths that walk
 *  them -- go through this, so a packet cannot fly down a lane the link it is
 *  following was not drawn in. */
function linkRouter(
  pairs: [PlacedCard, PlacedCard][],
  kind: FloorKind,
  floor: PlacedCard[],
): (a: PlacedCard, b: PlacedCard) => Geometry {
  const pairKey = (a: PlacedCard, b: PlacedCard) =>
    a.nodeId < b.nodeId ? `${a.nodeId} ${b.nodeId}` : `${b.nodeId} ${a.nodeId}`
  const plans = new Map<string, Plan>()
  const claims = new Map<string, Claim>()
  for (const [a, b] of pairs) {
    const key = pairKey(a, b)
    if (plans.has(key)) continue
    const plan = planRoute(a, b, kind, floor)
    plans.set(key, plan)
    plan.claims.forEach((c, i) => claims.set(`${key}#${i}`, c))
  }
  const lanes = assignLanes(claims)
  const cache = new Map<string, Geometry>()
  return (a, b) => {
    const key = pairKey(a, b)
    const hit = cache.get(key)
    if (hit) return hit
    // A pair nobody asked to route -- a deployment spanning a link the floor
    // suppressed. It gets a plan of its own and the sole lane in every gap it
    // needs, which is right: nothing else was drawn there to share with.
    const known = plans.has(key)
    const plan = plans.get(key) ?? planRoute(a, b, kind, floor)
    const dealt = plan.claims.map(
      (_, i) => (known ? lanes.get(`${key}#${i}`) : null) ?? { i: 0, of: 1 },
    )
    const pts = plan.build(
      plan.claims.map((c, i) => c.place(dealt[i]!)),
      dealt,
    )
    const geo: Geometry = plan.chord
      ? {
          facing: false,
          chord: true,
          d: `M${pts[0]!.x} ${pts[0]!.y} L${pts[1]!.x} ${pts[1]!.y}`,
          dRender: `M${pts[0]!.x} ${pts[0]!.y} L${pts[1]!.x} ${pts[1]!.y}`,
          mid: longestRun(pts),
          pts,
        }
      : { facing: plan.facing, chord: false, ...rectilinear(pts), dRender: '', mid: { x: 0, y: 0 } }
    if (!plan.chord) {
      geo.mid = longestRun(geo.pts)
      // From the squared-off polyline, never from the waypoints: a hand-placed
      // plate can hand `rectilinear` a diagonal step, and the corner it adds to
      // get rid of one is a corner the wire has to turn.
      geo.dRender = roundedPath(geo.pts, CORNER_R)
    }
    cache.set(key, geo)
    return geo
  }
}

/** Coordinator first, then lexicographic. Still a pure function of the node
 *  set, and the machine everything else hangs off lands in the first slot. */
function defaultArrangement(nodes: TopologyNode[]): string[] {
  return [...nodes]
    .sort(
      (a, b) =>
        (a.role === 'coordinator' ? 0 : 1) - (b.role === 'coordinator' ? 0 : 1) ||
        a.node_id.localeCompare(b.node_id),
    )
    .map((n) => n.node_id)
}

/** Reconcile the persisted drag order against who is actually here. Ids of
 *  departed nodes are dropped from the RESULT but kept in storage, so a node
 *  that leaves and rejoins returns to its slot; nodes that joined since the
 *  drag are appended in default order rather than jumping to the front. */
export function reconcileOrder(nodes: TopologyNode[], order: string[] | null): string[] {
  const fallback = defaultArrangement(nodes)
  if (!order || order.length === 0) return fallback
  const present = new Set(fallback)
  const known = new Set(order)
  return [...order.filter((id) => present.has(id)), ...fallback.filter((id) => !known.has(id))]
}

export function layoutCluster(input: ClusterLayoutInput): ClusterLayout {
  // Authored units throughout. 480 is the rendered 640 floor, divided.
  const GW = Math.max(Math.round(640 / GSCALE), Math.round((input.width || 700) / GSCALE))
  // The viewBox must match the element's aspect exactly or preserveAspectRatio
  // letterboxes it and the dead space this whole arrangement exists to remove
  // comes straight back. Deriving GH from GW and the measured ratio keeps the
  // two equal by construction, even where GW is pinned at its 480 floor on a
  // narrow panel and no longer tracks the measured width.
  const GH = Math.max(1, Math.round((GW * (input.height || 420)) / (input.width || 700)))
  const arrangement = reconcileOrder(input.nodes, input.order)
  // Every question below about "what is deployed" is asked of this and never
  // of `input.deployments`: which machine is occupied, which links a plan
  // relies on, which bands are drawn, and which served names a provider is a
  // backup FOR. A dead row answering any one of them is the same lie in a
  // different place.
  const deployments = runners(input.deployments)
  const n = arrangement.length
  const tier = tierFor(n)
  // Only full-tier plates have room for the identity line, and only a floor
  // that actually needs one pays for it -- a cluster where every machine goes
  // by its node_id looks exactly as it did before this existed.
  const subline =
    tier === 'full' && input.nodes.some((node) => nodeSubtitle(node, node.hostname))
      ? SUBLINE_H
      : 0
  const heightOf = (t: Tier) => CARD[t].h + (t === 'full' ? subline : 0)
  const present = new Set(arrangement)
  // What each machine is running, which shares the plate's top line with its
  // name and is the pair that used to collide.
  const occupantOf = (nodeId: string) =>
    plateOccupant(
      [
        ...new Set(
          deployments.filter((d) => d.node_ids.includes(nodeId)).map((d) => d.served_name),
        ),
      ].sort(),
    )
  // The largest body any plate on this floor can be asked to draw: selection
  // promotes one in place, and the promoted plate keeps the floor's width.
  const grownTier: Tier = tier === 'chip' ? 'compact' : 'full'
  // Width is content-driven but floor-WIDE, for the reason SUBLINE_H is:
  // a row is as wide as its widest plate, and letting only the long-named
  // machines grow would put every plate's meter at a different x, which reads
  // as a rendering fault rather than as data.
  const card = {
    w: Math.max(
      CARD[tier].w,
      ...input.nodes
        .filter((node) => present.has(node.node_id))
        .map((node) => Math.ceil(plateNeed(node, occupantOf(node.node_id), grownTier, subline))),
    ),
    h: heightOf(tier),
  }
  const kind: FloorKind = n > RING_ABOVE ? 'ring' : 'grid'

  // Hand-placement, sanitised once here rather than trusted at every read.
  // The store is localStorage, which anyone can hand-edit and any older build
  // can have written, so a NaN or a number four screens wide has to mean "not
  // moved" rather than a floor that cannot be drawn.
  const offsetOf = (nodeId: string): Point => {
    const o = input.offsets?.[nodeId]
    if (!o || !Number.isFinite(o.x) || !Number.isFinite(o.y)) return { x: 0, y: 0 }
    const bound = (v: number) => Math.max(-OFFSET_LIMIT, Math.min(OFFSET_LIMIT, v))
    return { x: bound(o.x), y: bound(o.y) }
  }
  const handPlaced = arrangement.some((nodeId) => {
    const o = offsetOf(nodeId)
    return o.x !== 0 || o.y !== 0
  })

  const cards: PlacedCard[] = []
  const slots: Point[] = []
  const edges: ClusterEdge[] = []
  const bands: ClusterBand[] = []
  const conns: ClusterConn[] = []
  const junctions: { x: number; y: number; r: number; opacity: number }[] = []
  const paths: Record<string, Point[]> = {}

  if (n === 0) {
    return {
      // No ink to frame: this path renders the message as a <p>, not the SVG.
      tier, kind, width: GW, height: GH, ink: { x: 0, y: 0, w: GW, h: GH }, offsetX: 0, card, subline,
      cards, edges, bands, conns, junctions, slots, paths,
      entries: [], exits: [], provider: null,
      arrangement,
      emptyMessage: 'No machines yet. A Spark on this network appears here on its own.',
      suppressedPairs: 0,
    }
  }

  // Pairs a deployment is actually relying on. Needed before placement, since
  // whether any link hops over a plate decides how much headroom the floor
  // reserves.
  const spannedPairs = new Set<string>()
  for (const dep of deployments) {
    const ids = dep.node_ids.filter((idv) => present.has(idv))
    for (let i = 0; i + 1 < ids.length; i++) spannedPairs.add(edgeKey(ids[i]!, ids[i + 1]!))
  }
  const showsPair = (l: TopologyEdge) =>
    edgeMeasured(l) ||
    spannedPairs.has(edgeKey(l.src, l.dst)) ||
    n <= 4 ||
    input.selection.selNode === l.src ||
    input.selection.selNode === l.dst

  const floorAvail = Math.max(card.w, GW - FLOOR_X - EXIT_RESERVE - MARGIN)
  let floorRight = FLOOR_X + floorAvail
  let machineBottom: number

  if (kind === 'grid') {
    const gx = GAP_X[tier]
    const gy = GAP_Y[tier]
    // Sized from the TIER's plate rather than the grown one: how many
    // machines a row holds is a fact about the floor and the panel, and a
    // deployment with a long name must not re-flow the arrangement under the
    // operator. A grid wider than the panel is scaled by the fit, never cut.
    const fits = Math.floor((floorAvail + gx) / (CARD[tier].w + gx))
    const cols = Math.max(1, Math.min(n, MAX_COLS[tier], Number.isFinite(fits) ? fits : 1))
    const rows = Math.ceil(n / cols)
    const gridW = cols * card.w + (cols - 1) * gx
    const originX = FLOOR_X + Math.max(0, (floorAvail - gridW) / 2)

    const slotOf = new Map(arrangement.map((nodeId, i) => [nodeId, i]))
    const willArc = input.links.some((l) => {
      const i = slotOf.get(l.src)
      const j = slotOf.get(l.dst)
      if (i == null || j == null) return false
      if (Math.floor(i / cols) !== Math.floor(j / cols)) return false
      if (Math.abs((i % cols) - (j % cols)) === 1) return false
      return showsPair(l)
    })
    const topPad = willArc ? ARC_HEADROOM : MARGIN

    // Selection GROWS the plate in place rather than opening anything over the
    // drawing, so a row is only as tall as its tallest plate and the rows
    // below shift down. That is the mockup's own rule ("height is data-driven
    // so the selected node grows in place") and it is why row tops accumulate
    // instead of being row * pitch.
    const bodyTierOf = (nodeId: string): Tier =>
      nodeId === input.selection.selNode && tier !== 'full'
        ? tier === 'chip'
          ? 'compact'
          : 'full'
        : tier
    const rowHeights = Array.from({ length: rows }, (_, r) =>
      Math.max(
        ...arrangement
          .slice(r * cols, r * cols + cols)
          .map((nodeId) => heightOf(bodyTierOf(nodeId))),
      ),
    )
    const rowTop = (r: number) =>
      topPad + rowHeights.slice(0, r).reduce((a, h) => a + h + gy, 0)

    arrangement.forEach((nodeId, i) => {
      const row = Math.floor(i / cols)
      const col = i % cols
      const bodyTier = bodyTierOf(nodeId)
      const x = originX + col * (card.w + gx)
      const y = rowTop(row)
      slots.push({ x, y })
      cards.push({
        nodeId, x, y,
        w: card.w,
        h: heightOf(bodyTier),
        slot: i, row, col,
        offset: { x: 0, y: 0 },
        selected: nodeId === input.selection.selNode,
        bodyTier,
      })
    })

    floorRight = Math.max(originX + gridW, FLOOR_X + card.w)
    machineBottom = rowTop(rows - 1) + rowHeights[rows - 1]!
  } else {
    const radius = Math.max(120, (n * (card.w + 30)) / (2 * Math.PI))
    const w = Math.ceil(2 * (radius + card.w / 2))
    const h = Math.ceil(2 * (radius + card.h / 2))
    const cx = FLOOR_X + Math.max(w, floorAvail) / 2
    const cy = MARGIN + h / 2
    arrangement.forEach((nodeId, i) => {
      const angle = -Math.PI / 2 + (i / n) * 2 * Math.PI
      const x = cx + radius * Math.cos(angle) - card.w / 2
      const y = cy + radius * Math.sin(angle) - card.h / 2
      slots.push({ x, y })
      cards.push({
        nodeId, x, y, w: card.w, h: card.h, slot: i, row: 0, col: i,
        offset: { x: 0, y: 0 },
        selected: nodeId === input.selection.selNode,
        bodyTier: tier,
      })
    })
    floorRight = Math.max(FLOOR_X + w, FLOOR_X + card.w)
    machineBottom = MARGIN + h
  }

  // ── Hand-placement, applied once ─────────────────────────────────────────
  //
  // Here, after the slots are dealt and before anything at all is measured off
  // a plate. Links, band spans, leads, flight paths and the ink box are every
  // one of them read off `cards`, so displacing the plates in one pass at the
  // top is the whole of what free placement costs the rest of this function.
  //
  // `slots` is deliberately NOT displaced. It stays the home grid: where a
  // plate returns to when its offset is cleared, and what the keyboard reorder
  // deals into.
  if (handPlaced) {
    for (const c of cards) {
      const off = offsetOf(c.nodeId)
      c.offset = off
      c.x += off.x
      c.y += off.y
    }
    // The bands hang below the machines, so the lowest machine is where the
    // machines end -- not the lowest ROW, which is all the grid above knew.
    // Without this a plate dragged down is drawn on top of the band it feeds.
    machineBottom = Math.max(machineBottom, ...cards.map((c) => c.y + c.h))
  }

  // The ring centres on max(w, floorAvail)/2 but sizes floorRight from `w`
  // alone, so a ring narrower than the space it was given puts its rightmost
  // plate PAST floorRight. Nothing noticed while floorRight only sized the
  // off-cluster bands, which are drawn under the plates; the return rail is
  // measured from it and would have been drawn straight through a machine.
  floorRight = Math.max(floorRight, ...cards.map((c) => c.x + c.w))

  const placed = new Map(cards.map((c) => [c.nodeId, c]))

  // ── Links ────────────────────────────────────────────────────────────────
  //
  // /api/topology returns a COMPLETE graph -- every pair, measured or not
  // (control_plane/gateway/internal_api.py:79-99). That is 6 edges at four
  // machines but 66 at twelve, of which realistically one or two carry a
  // figure. Drawing all 66 is a hairball that hides the one that matters.
  let suppressedPairs = 0

  // Routed as a set, not one at a time. Which lane a link's cross-run gets
  // depends on the other links crossing the same gap, so the drawn pairs have
  // to be known before any of them has a shape -- see `linkRouter`.
  const drawnPairs: [PlacedCard, PlacedCard][] = []
  // The drawn wires, kept with the polyline each was drawn from, so the
  // crossing pass below can ask where they cross without re-deriving it from
  // a path string.
  const routed: RoutedEdge[] = []
  for (const link of input.links) {
    const a = placed.get(link.src)
    const b = placed.get(link.dst)
    if (a && b && a.nodeId !== b.nodeId && showsPair(link)) drawnPairs.push([a, b])
  }
  const route = linkRouter(drawnPairs, kind, cards)

  for (const link of input.links) {
    const a = placed.get(link.src)
    const b = placed.get(link.dst)
    if (!a || !b || a.nodeId === b.nodeId) continue
    if (!showsPair(link)) {
      suppressedPairs++
      continue
    }

    const key = edgeKey(link.src, link.dst)
    const measured = edgeMeasured(link)
    const geo = route(a, b)
    const selected = input.selection.selLink === key
    const relied = spannedPairs.has(key)
    const incident = input.selection.selNode === link.src || input.selection.selNode === link.dst
    const fraction = measured ? Math.min(1, link.all_reduce_gbps! / TP_THRESHOLD) : 0
    const label = measured ? `${link.all_reduce_gbps!.toFixed(1)} of ${TP_THRESHOLD} GB/s` : 'never measured'

    // The mockup's bracket: a 7-tall bar filling the channel between two
    // facing plates, top edge at a.y+19 so it lines up with the meters inside
    // them. Only where there IS a channel -- a bracket cannot span an elbow.
    const horizontal = kind === 'grid' && level(a, b)
    const bracket: ClusterBracket | null =
      geo.facing && horizontal
        ? {
            x: Math.min(a.x + a.w, b.x + b.w),
            y: Math.min(a.y, b.y) + 19,
            w: Math.abs(a.x <= b.x ? b.x - (a.x + a.w) : a.x - (b.x + b.w)),
            h: 7,
            fraction,
            hitY: Math.min(a.y, b.y) + 14,
            hitH: 18,
          }
        : null

    const edge: ClusterEdge = {
      linkKey: key,
      src: link.src,
      dst: link.dst,
      kind: bracket ? 'bracket' : 'path',
      d: bracket ? '' : geo.d,
      dRender: bracket ? '' : geo.dRender,
      pts: bracket ? [] : geo.pts,
      bracket,
      width: measured ? edgeWidth(link.all_reduce_gbps!) : 1,
      dashed: !measured,
      // Opacity is the deprioritized channel. An unmeasured pair nothing runs
      // over is background; one a deployment depends on is not.
      opacity: measured ? 1 : relied || selected || incident ? 0.7 : 0.35,
      measured,
      label,
      labelAt: bracket
        ? { x: bracket.x + bracket.w / 2, y: Math.min(a.y, b.y) - 8 }
        : geo.mid,
      showLabel: measured || relied || selected || incident,
      selected,
      // `medium` is declared required in types.ts but the gateway omits it for
      // a pair it has never probed, so it is only spoken when it is there.
      aria: measured
        ? `Link ${link.src} to ${link.dst}, ${link.all_reduce_gbps!.toFixed(1)} gigabytes per second all-reduce${link.medium ? ` over ${link.medium}` : ''}`
        : `Link ${link.src} to ${link.dst}${link.medium ? ` over ${link.medium}` : ''}, never measured`,
    }
    edges.push(edge)
    // A bracket fills a channel rather than crossing one, so it has nothing to
    // break and nothing to be broken by.
    if (!bracket && !geo.chord) {
      routed.push({ edge, pts: geo.pts, rank: measured ? link.all_reduce_gbps! : -1 })
    }
  }

  // Now that every wire has a shape, and only now: which of them cross is a
  // question about the drawing as a whole, the same way `assignLanes` is.
  breakAtCrossings(routed)

  // Two captions landing on top of each other is worse than one of them
  // moving. Nudge in draw order; the first one placed keeps its spot.
  // Measured rather than assumed 44 wide: a caption is a plate sized to its
  // own text (`plateWidth`), so `10.2 of 40 GB/s` and `never measured` need
  // different amounts of room to clear each other, and the fixed guess let
  // the wide pair overlap while it moved the narrow pair that did not have to.
  const taken: { x: number; y: number; w: number }[] = []
  for (const e of edges) {
    if (!e.showLabel) continue
    const w = plateWidth(e.label)
    let guard = 0
    while (
      taken.some(
        (p) => Math.abs(p.x - e.labelAt.x) < (p.w + w) / 2 && Math.abs(p.y - e.labelAt.y) < 13,
      ) &&
      guard < 6
    ) {
      e.labelAt = { x: e.labelAt.x, y: e.labelAt.y - 14 }
      guard++
    }
    taken.push({ x: e.labelAt.x, y: e.labelAt.y, w })
  }

  // ── Bands: one per deployment, including single-node ones ────────────────
  //
  // The band IS the mockup's served-name plate. A solo deployment gets one
  // spanning its single machine, because that is what gives it somewhere to
  // sit in the request flow -- the entry box connects to bands, never to
  // machines.
  const bandTop = machineBottom + BAND_LEAD_SPACE
  const drawable = deployments
    .filter((d) => d.node_ids.some((id) => placed.has(id)))
    .sort((a, b) => a.served_name.localeCompare(b.served_name) || a.deployment_id.localeCompare(b.deployment_id))

  // What a provider serves, sorted into what belongs on the floor and what is
  // catalogue. `deployments` rather than `drawable` decides what counts
  // as backed up: a deployment whose machines are not on this floor is still a
  // local deployment of that name, and drawing the provider as though it were
  // the only thing serving it would be a lie about where the traffic goes.
  const remoteGroups = groupRemotes(
    input.remotes ?? [],
    new Set(deployments.map((d) => d.served_name)),
  )
  const backupsFor = new Map<string, string[]>()
  for (const g of remoteGroups) {
    if (g.role === 'backup') backupsFor.set(g.servedName, g.providers)
  }
  // What the bus box will say, worked out here rather than where it is drawn:
  // the box spans the floor, so the floor cannot be sized until its widest row
  // is known. Only the words are settled here; which of them are live is a
  // question about bands, and is answered further down.
  const hasProviderBus =
    input.routing.some((cfg) => cfg.targets.some((t) => t.kind === 'remote')) ||
    remoteGroups.length > 0
  // One line per provider, naming the models it serves HERE. The box used to
  // carry one aggregate sentence listing every routed name with no way to say
  // which provider answered for which -- with two providers on the rail it
  // could not answer the question it exists to answer.
  const drawnBy = new Map<string, string[]>()   // provider -> names on the floor
  const reachable = new Map<string, number>()   // provider -> names not drawn
  for (const g of remoteGroups) {
    for (const pid of g.providers) {
      if (g.role === 'catalog') {
        reachable.set(pid, (reachable.get(pid) ?? 0) + 1)
      } else {
        drawnBy.set(pid, [...(drawnBy.get(pid) ?? []), g.servedName])
      }
    }
  }
  // Providers with no topology rows at all still belong on this box: the rail
  // exists because routing says a remote target does, and a provider that
  // answers /api/routing but not /api/topology is a coordinator skew, not an
  // absence.
  const fromRouting = new Set(
    input.routing
      .flatMap((cfg) => cfg.targets.filter((t) => t.kind === 'remote'))
      .map((t) => t.target_id.split(':')[0] ?? t.target_id),
  )
  const providerIds = [...new Set([...drawnBy.keys(), ...reachable.keys(), ...fromRouting])].sort()
  const providerRowTexts = providerIds.map((pid) => {
    const names = (drawnBy.get(pid) ?? []).sort()
    const more = reachable.get(pid) ?? 0
    // Three names, then a count. A provider serving eight would otherwise set
    // the width of the whole box.
    const shown = names.slice(0, 3).join(', ')
    const rest = names.length > 3 ? `, and ${names.length - 3} more` : ''
    // "nothing routed here yet" is only true when this provider serves nothing
    // at all -- a rail that exists because /api/routing named a remote target
    // the topology has no row for. A whole catalogue switched on reaches this
    // same branch, because a catalogue earns no band and so no drawn name, and
    // the box then read "nothing routed here yet · 428 more reachable": one
    // clause denying what the next one counts. Every one of those 428 is
    // routed; what they are not is drawn, which is the floor's rule about
    // catalogues and not a fact about routing.
    const serves = names.length
      ? [`${shown}${rest}`, more ? `${more} more reachable` : '']
      : [more ? `${more} reachable, none drawn here` : 'nothing routed here yet']
    return {
      providerId: pid,
      text: [pid, ...serves].filter(Boolean).join(' · '),
    }
  })
  // The mockup's line 1 carries an aggregate price. There is no provider-level
  // price on the wire to fill that clause with, but "no telemetry" is not a
  // cost figure and stays on this line.
  const providerLabel = `${
    providerIds.length === 1 ? providerIds[0]! : `${providerIds.length} providers`
  } · third party · no telemetry`
  const providerSublabel = 'proxy for any served name'
  const providerBoxNeed = hasProviderBus
    ? providerNeed(providerLabel, providerSublabel, providerRowTexts.map((r) => r.text))
    : 0

  const plates = (ids: string[]) =>
    ids
      .map((id) => placed.get(id))
      .filter((c): c is PlacedCard => c != null)
      // Reading order in the DRAWING, which on the default grid is exactly the
      // row/col order this used to sort by -- row tops ascend with the row and
      // slot x with the column -- and which stays reading order once a plate
      // has been moved somewhere its slot index knows nothing about.
      .sort((p, q) => p.y - q.y || p.x - q.x)
  // A hosted model whose machine is not on this floor has nothing to sit on,
  // so it falls back to the off-cluster group rather than to a band spanning
  // plates that are not there.
  const hosted = remoteGroups.filter((g) => g.role === 'hosted' && plates(g.hostIds).length > 0)
  const offCluster = [
    ...remoteGroups.filter((g) => g.role === 'hosted' && plates(g.hostIds).length === 0),
    ...remoteGroups.filter((g) => g.role === 'named'),
  ].sort((a, b) => a.servedName.localeCompare(b.servedName))

  /** The span an occupied set of plates gives a band. Shared by every band on
   *  the upper floor, local or hosted: a model running on your Ollama box gets
   *  the same bar, ticks and leads as one your cluster is serving, because it
   *  is the same fact about the same machines. */
  const spanOf = (occupied: PlacedCard[]) => {
    // Never empty: both callers filter to bands with a plate on this floor.
    const head = occupied[0]!
    const sameRow = kind === 'grid' && occupied.every((c) => level(c, head))
    const left = sameRow ? Math.min(...occupied.map((c) => c.x)) : FLOOR_X
    const right = sameRow ? Math.max(...occupied.map((c) => c.x + c.w)) : floorRight
    // "Consecutive columns" was a proxy for the question this now asks
    // outright: does the bar drawn between the leftmost and rightmost machine
    // run across a machine that is not one of this band's own? On the default
    // grid the two are the same answer -- the plate in an intervening column
    // IS the intruder -- and only this one survives a plate being placed by
    // hand, where a column index no longer says what the bar crosses.
    const mine = new Set(occupied.map((c) => c.nodeId))
    const contiguous =
      sameRow &&
      !cards.some(
        (c) => !mine.has(c.nodeId) && level(c, head) && c.x + c.w > left && c.x < right,
      )
    return {
      contiguous,
      sameRow,
      // A band across two rows is given the floor rather than a span, so it
      // has to be re-stretched once the floor's final width is known.
      fullFloor: !sameRow,
      x: left,
      w: right - left,
      ticks: occupied.map((c) => c.x + c.w / 2),
      members: occupied.map((c) => c.nodeId),
    }
  }

  /** One polyline per occupied plate, from its own bottom-centre into this
   *  band's top edge -- a straight two-point run while the band sits where
   *  it was packed, still directly below its members, exactly the fixed
   *  vertical segment this always drew before a band could be hand-placed.
   *  Once `offset` has moved the band out from under them, an elbow: down
   *  partway, across to whichever of the band's edges is nearest, then into
   *  its top -- the same shape every other connector on this floor uses to
   *  reach a box it is not aligned with (see the entry/exit connectors
   *  below), rather than a line that would otherwise run in diagonally. */
  const bandLeads = (occupied: PlacedCard[], bandX: number, bandW: number, bandTopY: number): Point[][] =>
    occupied.map((c) => {
      const mx = c.x + c.w / 2
      const my = c.y + c.h
      if (mx >= bandX && mx <= bandX + bandW) return [{ x: mx, y: my }, { x: mx, y: bandTopY }]
      const midY = (my + bandTopY) / 2
      const targetX = mx < bandX ? bandX : bandX + bandW
      return [
        { x: mx, y: my },
        { x: mx, y: midY },
        { x: targetX, y: midY },
        { x: targetX, y: bandTopY },
      ]
    })

  // Bands given the whole floor before the floor knew how wide it was going
  // to be, re-stretched at the end of this section.
  const stretched: ClusterBand[] = []

  // A running cursor rather than `bandTop + i * (BAND_H + BAND_GAP)`: bands no
  // longer all have the same height, because one that is still arriving grows
  // to hold its stepper. Index arithmetic would stack the band after it back
  // on top of it.
  let bandY = bandTop

  drawable.forEach((dep) => {
    const occupied = plates(dep.node_ids)
    const naturalY = bandY
    const span = spanOf(occupied)
    // Hand-placement, both axes -- unlike a machine's, this offset is looked
    // up before the band's own final geometry exists, because the leads
    // below have to be routed against where it actually ends up, not where
    // it was packed.
    const bandOffset = input.offsets?.[dep.deployment_id] ?? { x: 0, y: 0 }
    const x = span.x + bandOffset.x
    const y = naturalY + bandOffset.y
    const backups = backupsFor.get(dep.served_name) ?? []
    const plan = dep.plan ? planShortFromDegrees(dep.plan) : ''
    // The backup is named on the band it backs, which is the only place the
    // relation is legible: this name is served here AND, once every local
    // slot stops admitting, over there.
    const sublabel = [
      span.contiguous ? '' : span.members.join(', '),
      backups.length ? `${providersLabel(backups)} backup` : '',
    ]
      .filter(Boolean)
      .join(' · ')
    const degraded = dep.state === 'degraded'
    const loading = isArriving(dep.state)
    const h = loading ? BAND_LOADING_H : BAND_H
    // Its span, or what its own words need -- whichever is larger. The bar
    // has always been as wide as the machines it occupies, which has nothing
    // to do with how long the model's name is, and the served name is the
    // band's identity: where the two disagree the bar grows past its
    // machines and the leads under it go on saying which ones are its own.
    const w = Math.max(
      span.w,
      bandNeed(dep.served_name, bandSubline({ plan, sublabel, degraded })),
      // Reserved whether or not a pull is reporting one, so the detail
      // column has somewhere to arrive.
      loading ? bandLoadingNeed(Object.values(PHASE_LABEL), '000.0 / 000.0 GB') : 0,
    )

    const band: ClusterBand = {
      id: dep.deployment_id,
      deploymentId: dep.deployment_id,
      kind: 'local',
      targetIds: [],
      providers: backups,
      offCluster: false,
      servedName: dep.served_name,
      modality: dep.modality ?? 'text',
      x,
      y,
      w,
      h,
      ny: y + h / 2,
      contiguous: span.contiguous,
      ticks: span.ticks,
      members: span.members,
      leads: span.sameRow ? bandLeads(occupied, x, w, y) : [],
      plan,
      sublabel,
      degraded,
      loading,
      selected: input.selection.selDep === dep.served_name,
      offset: bandOffset,
    }
    bands.push(band)
    bandY += h + BAND_GAP
    if (span.fullFloor) stretched.push(band)
  })

  // Hosted remotes join the upper floor, because they are not off it: the
  // provider is a machine on this roster and the model is running on a plate
  // already drawn. The request never leaves the building.
  hosted.forEach((g) => {
    const occupied = plates(g.hostIds)
    const naturalY = bandY
    const span = spanOf(occupied)
    const bandOffset = input.offsets?.[`remote:${g.servedName}`] ?? { x: 0, y: 0 }
    const x = span.x + bandOffset.x
    const y = naturalY + bandOffset.y
    const plan = `via ${providersLabel(g.providers)}`
    const sublabel = span.contiguous ? '' : span.members.join(', ')
    // A provider that stopped answering is the same fault as a deployment
    // that did, and reads as one: the band, not a separate vocabulary.
    const degraded = !g.healthy || !g.admitting
    const w = Math.max(span.w, bandNeed(g.servedName, bandSubline({ plan, sublabel, degraded })))
    const band: ClusterBand = {
      id: `remote:${g.servedName}`,
      deploymentId: null,
      kind: 'remote',
      targetIds: g.targetIds,
      providers: g.providers,
      offCluster: false,
      servedName: g.servedName,
      modality: g.modality,
      x,
      y,
      w,
      h: BAND_H,
      ny: y + BAND_H / 2,
      contiguous: span.contiguous,
      ticks: span.ticks,
      members: span.members,
      leads: span.sameRow ? bandLeads(occupied, x, w, y) : [],
      plan,
      sublabel,
      degraded,
      // A hosted remote is somebody else's process on a machine of ours: no
      // deployment of ours is arriving, so there is no launch to step through.
      loading: false,
      selected: input.selection.selDep === g.servedName,
      offset: bandOffset,
    }
    bands.push(band)
    bandY += BAND_H + BAND_GAP
    if (span.fullFloor) stretched.push(band)
  })

  // ── How wide the floor turned out to be ──────────────────────────────────
  //
  // The plates set a first edge, but three things are measured in words
  // rather than in machines and any of them can reach further right: a band
  // grown to fit its served name, an off-cluster band, and the bus box's
  // longest row. `floorRight` is what the return column, the provider drop
  // and every full-width band are measured from, so it is settled here, once,
  // before the first of them is placed -- an edge fixed to the grid would put
  // the return rail straight through a band that outgrew it.
  const offClusterNeed = Math.max(
    0,
    ...offCluster.map((g) =>
      bandNeed(
        g.servedName,
        bandSubline({
          plan: `via ${providersLabel(g.providers)}`,
          sublabel: 'off cluster',
          degraded: !g.healthy || !g.admitting,
        }),
      ),
    ),
  )
  floorRight = Math.max(
    floorRight,
    ...bands.map((b) => b.x + b.w),
    FLOOR_X + offClusterNeed,
    FLOOR_X + providerBoxNeed,
  )
  for (const b of stretched) b.w = floorRight - b.x

  const upperBottom = bands.length ? bandY - BAND_GAP : machineBottom

  // ── The bands below the floor, and the provider bus ──────────────────────
  //
  // A provider is ONE endpoint that can serve anything, so it is drawn as a
  // bus: a rail tapping every served name and a single trunk into the box.
  // It is drawn like a machine plate now -- filled, hairline border, can be
  // hand-placed the same way (ClusterProvider.offset, vertical only: the box
  // spans the full floor width by construction, so a horizontal offset has
  // nowhere honest to go). What still marks it as "not yours" is what a
  // machine plate could never show truthfully for one: no memory meter, no
  // signal-coloured border, no served-name occupant line -- and the mark on
  // each provider row, which says it better than an outline ever did.
  //
  // What IS drawn like a machine's is the served name in front of it. A model
  // only a provider answers for gets the same band as one this cluster runs,
  // below the local ones rather than above them, but the same bar with the
  // same name, the same throughput readout and the same selection. It is the same served name in the same request flow; the
  // only honest differences are that it has no plan and no machine of ours
  // under it, and the drawing says exactly those two things and nothing more.
  const remoteOf = (servedName: string) => {
    const cfg = input.routing.find((c) => c.served_name === servedName)
    const t = cfg?.targets.find((x) => x.kind === 'remote')
    return t ?? null
  }
  let provider: ClusterProvider | null = null

  const offTop = upperBottom + 12
  offCluster.forEach((g, i) => {
    const naturalY = offTop + i * (BAND_H + BAND_GAP)
    // Vertical only, same reasoning as the provider bus: this box spans the
    // full floor width by construction, so a horizontal offset has nowhere
    // honest to go.
    const bandOffset = input.offsets?.[`remote:${g.servedName}`] ?? { x: 0, y: 0 }
    const y = naturalY + bandOffset.y
    bands.push({
      id: `remote:${g.servedName}`,
      deploymentId: null,
      kind: 'remote',
      targetIds: g.targetIds,
      providers: g.providers,
      offCluster: true,
      servedName: g.servedName,
      modality: g.modality,
      x: FLOOR_X,
      y,
      w: floorRight - FLOOR_X,
      h: BAND_H,
      ny: y + BAND_H / 2,
      // Nothing of ours is under it, so there is no span to be honest or
      // dishonest about: the bar is whole, and its own words say whose
      // hardware it is.
      contiguous: true,
      ticks: [],
      members: [],
      leads: [],
      plan: `via ${providersLabel(g.providers)}`,
      sublabel: 'off cluster',
      degraded: !g.healthy || !g.admitting,
      // Nothing of ours is arriving: the model is already up on somebody
      // else's hardware, which is the whole of what an off-cluster band is.
      loading: false,
      selected: input.selection.selDep === g.servedName,
      offset: { x: 0, y: bandOffset.y },
    })
  })
  const bandBottom = offCluster.length
    ? offTop + offCluster.length * (BAND_H + BAND_GAP) - BAND_GAP
    : upperBottom

  // ── Entry box, and the fan-out to the bands ──────────────────────────────
  //
  // The entry connects to BANDS, not to machines: that is the mockup's own
  // architecture (endpoint -> served name -> targets), and it is the only
  // routing that cannot cross a plate, since a connector aimed at column two
  // would have to pass through column one. A remote band is a band, so it is
  // entered the same way -- a request for a model a provider serves arrives
  // at the same endpoint as every other.
  //
  // ONE PLATE PER ENDPOINT FAMILY, and only for the families actually on the
  // floor. A speech deployment and a chat deployment are not two models behind
  // one endpoint -- they are two endpoints, and naming a TTS band's route
  // `POST /v1/chat/completions` draws a request the gateway refuses outright
  // (`wrong_modality`, gateway/openai_api.py). A cluster with nothing but chat
  // deployments, which is nearly all of them, is unchanged: one family, one
  // plate, at the same y it has always had.
  const groupsByModality = endpointGroups(bands)
  // Placed BEFORE anything is connected to them. Each plate wants the middle
  // of its own bands and two families whose bands interleave want the same
  // middle, so `separate` may have to shuffle one -- and a lead drawn from
  // where the plate wanted to be would leave a packet flying out of thin air
  // beside it. Position first, connect to the position.
  const entries: EndpointPlate[] = groupsByModality.map(([modality, members]) => {
    const ey = members[Math.floor(members.length / 2)]!.ny
    const label = ENTRY_LABELS[modality]
    const w = entryWidth(label)
    return { x: ENTRY_R - w, y: ey - ENTRY_H / 2, w, h: ENTRY_H, label, modality }
  })
  separate(entries, ENTRY_H)

  /** Where a band's own request enters, for the flight paths further down. A
   *  packet has to fly out of the plate its band actually hangs off, or the
   *  drawing animates a chat request turning into speech. */
  const entryY = new Map<string, number>()
  groupsByModality.forEach(([, members], i) => {
    const plate = entries[i]!
    const ey = plate.y + plate.h / 2
    for (const band of members) {
      entryY.set(band.id, ey)
      conns.push(
        connFrom(
          `entry-${band.id}`,
          [
            { x: ENTRY_R, y: ey },
            { x: GUTTER_ENTRY, y: ey },
            { x: GUTTER_ENTRY, y: band.ny },
            { x: band.x, y: band.ny },
          ],
          5,
          1,
        ),
      )
    }
  })

  // ── Exit box, and the return leg from every band ─────────────────────────
  //
  // The mirror of the entry, and the half of the picture that was missing: a
  // request arrived, walked its machines and then stopped, so the most
  // characteristic thing about serving a model -- tokens streaming back out
  // while more requests are still arriving -- was the one thing the drawing
  // could not show.
  //
  // The leg leaves from the band's RIGHT EDGE, not from the last machine.
  // Two reasons, and the first is the load-bearing one:
  //
  //   * A run from a plate centre would cross that plate's row-mates on its
  //     way out, which is the same reason the entry and the provider rail
  //     were both banished to gutters left of FLOOR_X. Every band sits below
  //     `machineBottom` and ends at or before `floorRight`, so a horizontal
  //     run at `band.ny` cannot cross a plate by construction.
  //   * The band's right edge is where its tok/s readout already sits, so the
  //     number and the blocks leaving are visibly the same measurement.
  // Grouped exactly as the entry is, and for the same reason: what comes back
  // off a speech band is one audio file, not a token stream, so a single plate
  // reading `200 text/event-stream` in front of both would be wrong about
  // whichever half it was not describing.
  const rail = floorRight + GUTTER_EXIT
  const ex = rail + EXIT_GAP
  const exits: EndpointPlate[] = groupsByModality.map(([modality, members]) => {
    const ey = members[Math.floor(members.length / 2)]!.ny
    const label = EXIT_LABELS[modality]
    return { x: ex, y: ey - EXIT_H / 2, w: exitWidth(label), h: EXIT_H, label, modality }
  })
  separate(exits, EXIT_H)

  groupsByModality.forEach(([, members], i) => {
    const plate = exits[i]!
    const ey = plate.y + plate.h / 2
    for (const band of members) {
      // One array, two consumers: the wire that is drawn and the flight that
      // walks it. They were spelled separately and had to agree by hand.
      const out: Point[] = [
        { x: band.x + band.w, y: band.ny },
        { x: rail, y: band.ny },
        { x: rail, y: ey },
        { x: ex, y: ey },
      ]
      conns.push(connFrom(`exit-${band.id}`, out, 5, 1))
      paths[outFlightKey(band.id)] = out
    }
  })

  // ── The provider bus ─────────────────────────────────────────────────────
  if (hasProviderBus) {
    // Hand-placement, vertical only -- see ClusterProvider.offset. Folded in
    // here, at the one place `provY` is computed, so `busY`/`outY`/the box
    // itself all move together and nothing downstream has to know a drag
    // happened at all.
    const providerOffsetY = input.offsets?.[PROVIDER_NODE_ID]?.y ?? 0
    const provY = bandBottom + 20 + providerOffsetY
    let anyActive = false
    const activeProviders = new Set<string>()

    // Where the drop runs, and where it lands: right of everything the floor
    // draws, then back into the box's own right edge, so the line ends exactly
    // where the flight below ends. They used to disagree -- the rail stopped
    // at the box's left edge while the block carried on to its centre -- which
    // is how a request arrived at the provider by sliding out from under a
    // line and across the label.
    const dropX = floorRight + GUTTER_PROV
    const busY = provY + 19

    // Only a band a provider actually serves is connected to the bus. Every
    // band used to get a 0.22 hairline meaning "reachable through the proxy",
    // which was true when a provider served its whole catalogue and is not
    // true now: `enabled_models` means a provider serves nothing but its
    // allowlist, so a line from a name no provider offers claims routing that
    // cannot happen. `band.providers` carries the relation from both sides --
    // the backup on a local band and the provider behind a remote one -- and
    // it comes off the topology payload, which is already allowlist-filtered
    // server side, so this stays a pure function of the wire.
    const tapped = bands.filter((b) => b.providers.length > 0 || remoteOf(b.servedName) != null)

    for (const band of tapped) {
      const t = remoteOf(band.servedName)
      const active = t != null && (t.weight ?? 0) > 0 && band.selected
      if (active) {
        anyActive = true
        for (const pid of band.providers) activeProviders.add(pid)
      }
      // The band's BOTTOM-right corner, eighteen units below where the return
      // leg leaves the same edge. Two runs out of one point would read as one
      // wire forking, which is the opposite of what happens here: what leaves
      // at `ny` is the answer coming back, and what leaves here is the request
      // going on to somebody else's hardware.
      conns.push(
        connFrom(
          `prov-${band.id}`,
          [
            { x: band.x + band.w, y: band.y + band.h },
            { x: dropX, y: band.y + band.h },
          ],
          2.5,
          active ? 1 : 0.55,
        ),
      )
      junctions.push({
        x: band.x + band.w,
        y: band.y + band.h,
        r: 3,
        opacity: active ? 1 : 0.55,
      })
      const py = entryY.get(band.id) ?? band.ny
      paths[providerFlightKey(band.servedName)] = [
        { x: ENTRY_R, y: py },
        { x: GUTTER_ENTRY, y: py },
        { x: GUTTER_ENTRY, y: band.ny },
        // Across the band, the way a local request crosses its own before it
        // reaches a machine. The port lost this: the old path turned down the
        // gutter sixteen units short of the band, so a block for a name was
        // never once seen on that name.
        { x: band.x, y: band.ny },
        { x: band.x + band.w, y: band.ny },
        { x: band.x + band.w, y: band.y + band.h },
        { x: dropX, y: band.y + band.h },
        { x: dropX, y: busY },
        { x: floorRight, y: busY },
      ]
    }

    // One trunk, not one vertical per band: N runs down the same corridor
    // would state the same relation N times and stack their opacities into a
    // line darker than either of them.
    if (tapped.length) {
      const top = Math.min(...tapped.map((b) => b.y + b.h))
      conns.push(
        connFrom(
          'provider-trunk',
          [
            { x: dropX, y: top },
            { x: dropX, y: busY },
            { x: floorRight, y: busY },
          ],
          1,
          0.5,
        ),
      )
    }

    // ── And back out of it ───────────────────────────────────────────────
    //
    // The bus was a dead end: a request dropped into it and the drawing
    // stopped there, so a floor whose traffic goes to a provider drew the
    // whole outward half of the route and nothing of the return -- the one
    // box on the canvas that received and never answered.
    //
    // Somebody else's hardware produced those bytes, but they leave through
    // THIS gateway like every other response, so the run joins the same
    // return column every band's exit leg already uses: out of the box's own
    // right edge, up the rail, into the plate for the endpoint family that
    // asked. It is drawn at the drop's weight, not the exit legs', because it
    // is the drop's counterpart and not a second measured stream.
    //
    // NO FLIGHT WALKS IT, and that is the deliberate part. What a remote
    // target produces is already measured and already animated -- as
    // out-blocks leaving the band by its own tok/s readout (`outFlightKey`,
    // and `bandTokensPerSec` sums exactly the remote targets behind that
    // band). Emitting them here as well would draw one measurement in two
    // places and let a reader count the same tokens twice, which is the one
    // thing particles.ts's header refuses to do. This line says where the
    // bytes came from; the blocks stay where the number is.
    const outY = busY + PROVIDER_OUT_DY
    const answered = new Set(tapped.map((b) => b.modality))
    groupsByModality.forEach(([modality], i) => {
      if (!answered.has(modality)) return
      const plate = exits[i]!
      const ey = plate.y + plate.h / 2
      conns.push(
        connFrom(
          `provider-out-${modality}`,
          [
            { x: floorRight, y: outY },
            { x: rail, y: outY },
            { x: rail, y: ey },
            { x: ex, y: ey },
          ],
          2.5,
          anyActive ? 1 : 0.55,
        ),
      )
    })

    // The words were settled before the floor was sized (see above); all this
    // adds is which of them are live, which is a question about bands.
    const rows: ClusterProviderRow[] = providerRowTexts.map((r) => ({
      providerId: r.providerId,
      text: r.text,
      active: activeProviders.has(r.providerId),
    }))

    provider = {
      x: FLOOR_X,
      y: provY,
      // The floor was widened to hold the longest of these rows, so this is
      // the box that fits them rather than the box that clips them.
      w: floorRight - FLOOR_X,
      h: PROVIDER_H + PROVIDER_ROW_H * rows.length,
      active: anyActive,
      label: providerLabel,
      sublabel: providerSublabel,
      rows,
      offset: { x: 0, y: providerOffsetY },
    }
  }

  // ── Particle flight paths ────────────────────────────────────────────────
  //
  // One path per ROUTING TARGET of a served name, keyed BY that target's id,
  // so particles.ts can bind a flight to the target whose live request count
  // it is drawing rather than to a position in a list. A flight enters at the
  // endpoint, reaches the served name, then walks the deployment's pipeline in
  // node_ids order -- which is stage order, not a set -- so a request visibly
  // crosses the link the plan turned on.
  // Keyed by band id, which for a local band IS its deployment id. A remote
  // band has no deployment and never answers this lookup.
  const bandOf = new Map(bands.map((b) => [b.id, b]))
  const byName = new Map<string, DeploymentDTO[]>()
  for (const dep of drawable) {
    const list = byName.get(dep.served_name) ?? []
    list.push(dep)
    byName.set(dep.served_name, list)
  }

  for (const [servedName, deps] of byName) {
    for (const dep of deps) {
      const band = bandOf.get(dep.deployment_id)
      const hops = dep.node_ids.map((id) => placed.get(id)).filter((c): c is PlacedCard => c != null)
      if (!band || hops.length === 0) continue

      const first = hops[0]!
      const leadX = first.x + first.w / 2
      const ly = entryY.get(band.id) ?? band.ny
      const pts: Point[] = [
        { x: ENTRY_R, y: ly },
        { x: GUTTER_ENTRY, y: ly },
        { x: GUTTER_ENTRY, y: band.ny },
        { x: band.x, y: band.ny },
        { x: leadX, y: band.ny },
        centerOf(first),
      ]
      for (let h = 0; h + 1 < hops.length; h++) {
        const seg = route(hops[h]!, hops[h + 1]!).pts
        const next = centerOf(hops[h + 1]!)
        const last = seg[seg.length - 1]!
        const forward =
          Math.hypot(last.x - next.x, last.y - next.y) <= Math.hypot(seg[0]!.x - next.x, seg[0]!.y - next.y)
        pts.push(...(forward ? seg : [...seg].reverse()), next)
      }
      paths[localFlightKey(servedName, dep.deployment_id)] = pts
    }
  }

  // Centre the ink. The extremes are the boxes, not the connectors: a
  // connector only ever runs between two of them, and the provider rail's own
  // trunk sits inside the provider box's span.
  const boxes: { x: number; y: number; w: number; h: number }[] = [
    ...cards,
    ...bands,
    ...entries,
    // Forgetting the exits clips the whole return column out of the fit, and
    // does it silently -- the geometry is right, the drawing just cannot be
    // seen.
    ...exits,
  ]
  if (provider) boxes.push(provider)
  const inkL = Math.min(...boxes.map((b) => b.x))
  const inkR = Math.max(...boxes.map((b) => b.x + b.w))
  // Same reasoning down the other axis, which the fit transform needs. Padded
  // by the hover/selection ring the renderer draws 3 units outside every box,
  // so a fitted drawing does not clip its own rings against the viewBox edge.
  const inkT = Math.min(...boxes.map((b) => b.y)) - RING_PAD
  const inkB = Math.max(...boxes.map((b) => b.y + b.h)) + RING_PAD
  const ink = { x: inkL - RING_PAD, y: inkT, w: inkR - inkL + 2 * RING_PAD, h: inkB - inkT }
  // Centred at every size, including wider than the viewBox. It used to clamp
  // at zero so that an oversized drawing stayed pinned left "where panning can
  // still reach the rest of it", which was true before the fit existed and is
  // not now: `fit` scales the ink to the element and centres it on ink's own
  // middle, so the clamp could not pin anything -- all it did was throw the
  // drawing off centre at exactly the sizes that need the room most, which is
  // every floor whose plates grew to fit their words.
  const offsetX = Math.round(GW / 2 - (inkL + inkR) / 2)

  return {
    tier, kind, width: GW, height: GH, ink, offsetX, card, subline,
    cards, edges, bands, conns, junctions, slots, paths,
    entries, exits, provider,
    arrangement,
    emptyMessage: null,
    suppressedPairs,
  }
}

/** Move `nodeId` into `slot`, shifting everything between. What the keyboard
 *  reorder writes; a pointer drag writes an offset instead. */
export function moveToSlot(arrangement: string[], nodeId: string, slot: number): string[] {
  const from = arrangement.indexOf(nodeId)
  if (from < 0) return arrangement
  const to = Math.max(0, Math.min(arrangement.length - 1, slot))
  if (from === to) return arrangement
  const next = [...arrangement]
  next.splice(from, 1)
  next.splice(to, 0, nodeId)
  return next
}

