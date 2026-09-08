import { useEffect, useState } from 'react'
import type { DeploymentDTO } from '../../api/types'
import { useActivity } from '../../state/resources'
import { PHASE_MARK_W, PHASE_ROW_H, SWEEP_DY, SWEEP_H, type ClusterBand } from './layout'
import { clock, launchView, type LaunchStep } from './loading'

// A deployment that has not arrived yet, drawn on its own band.
//
// The band is the floor's object for "one served name across the machines it
// occupies", so it is where a launch belongs: the machines underneath it are
// already drawn, already named, and already have their own state border. What
// the band could not do was say that the thing it names is not answering yet
// -- it drew a throughput readout, and `fmt` rendered the missing figure as an
// em dash, which is honest and says nothing about why.
//
// The furniture is the setup screen's launch plate (`SetupTab.tsx`), moved on
// to the floor and re-drawn in SVG: the same four phases, the same three marks,
// the same sweeping bar that claims no position. A launch watched during first
// run and the same launch watched on the floor are visibly one event.
//
// What it will NOT do is state a step it cannot see. `loading.ts` holds that
// rule and `loading.check.mjs` pins it.

/** The indeterminate bar on a loading band.
 *
 *  Static geometry, so it lives on the band itself and not in the layer that
 *  polls: nothing here is a reading, and that is the entire point. `activity.ts`
 *  states the rule this obeys -- the runtime container fetches its own weights
 *  into the host's cache and the control plane cannot see inside it, so there
 *  is no percentage and there will not be one. A bar creeping at a rate
 *  somebody made up is read as an estimate and planned around; a sweep says
 *  "working" and claims no position. Same bar, same refusal, as the setup
 *  screen's `.setup-bar .is-open`. */
export function BandSweep({ band }: { band: ClusterBand }) {
  const w = band.w - 22
  const clipId = `bandsweep-${band.id.replace(/[^A-Za-z0-9_-]/g, '_')}`
  const gradId = `${clipId}-g`
  return (
    <>
      <defs>
        <clipPath id={clipId}>
          <rect x={band.x + 11} y={band.y + SWEEP_DY} width={w} height={SWEEP_H} rx={2} />
        </clipPath>
        <linearGradient id={gradId} x1="0" x2="1" y1="0" y2="0">
          <stop offset="0%" stopColor="var(--flow)" stopOpacity={0} />
          <stop offset="35%" stopColor="var(--flow)" stopOpacity={1} />
          <stop offset="65%" stopColor="var(--flow)" stopOpacity={1} />
          <stop offset="100%" stopColor="var(--flow)" stopOpacity={0} />
        </linearGradient>
      </defs>
      <rect
        x={band.x + 11}
        y={band.y + SWEEP_DY}
        width={w}
        height={SWEEP_H}
        rx={2}
        fill="var(--on-fill)"
        opacity={0.18}
      />
      <g clipPath={`url(#${clipId})`}>
        {/* Two rects, one per motion preference, because the still form is a
            different shape and not just the moving one held: a 40%-wide
            highlight parked at the left edge would read as 40% done, which is
            the one thing this bar must never say. */}
        <rect
          className="bandsweep-move"
          x={band.x + 11}
          y={band.y + SWEEP_DY}
          width={w * 0.4}
          height={SWEEP_H}
          fill={`url(#${gradId})`}
        />
        <rect
          className="bandsweep-still"
          x={band.x + 11}
          y={band.y + SWEEP_DY}
          width={w}
          height={SWEEP_H}
          fill="var(--flow)"
          opacity={0.45}
        />
      </g>
    </>
  )
}

/** The launch steppers, as one layer over the bands.
 *
 *  One component for every loading band rather than one per band, because it
 *  owns the two things that must not be duplicated: the `/api/activity` poll
 *  (`useResource` starts an interval per call site -- a hook inside `Band`
 *  would poll once per band) and the one-second clock. It is also why this is
 *  a layer instead of children of each `<g class="band">`: the doc block at the
 *  top of this file is emphatic that the scene must not re-render on live
 *  data, and a two-second poll threaded through `ClusterGraph` as a prop would
 *  rebuild every plate, edge and particle host with it. Drawn after the bands
 *  and before the plates, which is where the bands themselves are.
 *
 *  `pointer-events: none` on the group: the words sit on top of a band that is
 *  a button, and a click landing on a phase label rather than on the bar it is
 *  drawn inside would silently stop selecting the deployment. */
export function BandLaunchLayer({
  bands,
  deployments,
}: {
  bands: ClusterBand[]
  deployments: DeploymentDTO[]
}) {
  const activity = useActivity()
  const [now, setNow] = useState(() => Date.now() / 1000)

  // A second, because the clock under the steps counts in seconds. Only while
  // something is actually arriving: a floor with nothing loading on it does
  // not get a timer at all.
  const loading = bands.filter((b) => b.loading)
  const any = loading.length > 0
  useEffect(() => {
    if (!any) return
    // Read once on the way in as well as on every tick: this component mounts
    // with the floor and may sit for an hour before anything launches on it,
    // and a clock seeded at mount would open an hour behind and take a full
    // second to correct itself.
    setNow(Date.now() / 1000)
    const t = window.setInterval(() => setNow(Date.now() / 1000), 1000)
    return () => window.clearInterval(t)
  }, [any])

  if (!any) return null

  return (
    <g style={{ pointerEvents: 'none' }}>
      {loading.map((band) => {
        const dep = deployments.find((d) => d.deployment_id === band.deploymentId)
        if (!dep) return null
        const view = launchView(
          dep,
          activity.data?.launches.find((l) => l.deployment_id === dep.deployment_id) ?? null,
          activity.data?.downloads ?? [],
          now,
        )
        return <BandLaunch key={band.id} band={band} view={view} />
      })}
    </g>
  )
}

export function BandLaunch({
  band,
  view,
}: {
  band: ClusterBand
  view: ReturnType<typeof launchView>
}) {
  return (
    <>
      {/* Where the throughput readout would be. `null` elapsed draws nothing
          rather than 0:00 -- a launch this coordinator has no record of
          starting has been running for an unknown time, and a zeroed clock
          says it started this second. */}
      {view.elapsed != null ? (
        <>
          <text
            x={band.x + band.w - 11}
            y={band.y + 17}
            textAnchor="end"
            className="m"
            fontSize={13}
            fill="var(--on-fill)"
          >
            {clock(view.elapsed)}
          </text>
          <text
            x={band.x + band.w - 11}
            y={band.y + 29}
            textAnchor="end"
            className="m"
            fontSize={9}
            fill="var(--on-fill-dim)"
          >
            elapsed
          </text>
        </>
      ) : null}

      {view.steps.map((step, i) => (
        <PhaseRow
          key={step.phase}
          step={step}
          x={band.x + 11}
          right={band.x + band.w - 11}
          y={band.y + SWEEP_DY + SWEEP_H + 12 + i * PHASE_ROW_H}
        />
      ))}
    </>
  )
}

/** One step: its mark, its name, and a size when a size was reported.
 *
 *  A list, because it IS a sequence -- what has happened, what is happening,
 *  and what is still ahead. The three marks and the three weights are the
 *  setup screen's, unchanged, so a launch watched there and the same launch
 *  watched here are visibly the same event. */
export function PhaseRow({
  step,
  x,
  right,
  y,
}: {
  step: LaunchStep
  x: number
  right: number
  y: number
}) {
  const done = step.state === 'done'
  const now = step.state === 'now'
  const fill = done || now ? 'var(--on-fill)' : 'var(--on-fill-dim)'
  return (
    <g opacity={step.state === 'todo' ? 0.55 : 1}>
      <text
        x={x + PHASE_MARK_W / 2}
        y={y}
        textAnchor="middle"
        className="m"
        fontSize={9}
        fill={done ? 'var(--live-solid)' : fill}
      >
        {done ? '✓' : now ? '●' : '○'}
      </text>
      <text
        x={x + PHASE_MARK_W}
        y={y}
        className="m"
        fontSize={9}
        fill={fill}
        fontWeight={now ? 500 : undefined}
      >
        {step.label}
        {/* The waiting dots, one tspan each so they can fade in turn. CSS
            `content` animation is what the setup screen uses and there are no
            pseudo-elements on SVG text, so the three glyphs are real. */}
        {now ? (
          <>
            <tspan className="banddot banddot1">.</tspan>
            <tspan className="banddot banddot2">.</tspan>
            <tspan className="banddot banddot3">.</tspan>
          </>
        ) : null}
      </text>
      {step.detail ? (
        <text x={right} y={y} textAnchor="end" className="m" fontSize={9} fill={fill}>
          {step.detail}
        </text>
      ) : null}
    </g>
  )
}
