// design-pack: data-svg — the particle layer is an SVG group in flow
// coordinates that particles.ts writes rects into; React never reconciles it.
import { useContext, useMemo, useRef } from 'react'
import { ViewportPortal } from '@xyflow/react'
import type { RouteTarget, RoutingConfig } from '../../api/types'
import { useMetrics } from '../../state/metrics'
import { useRequestMix } from '../../state/resources'
import { useSelection } from '../../state/selection'
import type { Wire } from './doc'
import { PathRegistry } from './edges'
import { mostlyStreaming, streamingRatio, targetFlows, useParticleField } from './particles'

/** Blue blocks on request wires, one per request in flight on that target;
 *  teal blocks on response wires at a capped image of measured tokens/sec.
 *  Suppressed entirely under reduced motion (useParticleField). */
export function ParticleLayer({ wires, routing }: { wires: Wire[]; routing: RoutingConfig[] }) {
  const layerRef = useRef<SVGGElement>(null)
  const paths = useContext(PathRegistry) ?? {}
  const { frame, stale } = useMetrics()
  const { selDep } = useSelection()
  const mix = useRequestMix(selDep)

  const keyFor = useMemo(() => {
    const byTarget = new Map(
      wires.filter((w) => w.type === 'requests' && w.target_id).map((w) => [w.target_id!, w.id]),
    )
    return (_served: string, t: RouteTarget) => byTarget.get(t.target_id) ?? null
  }, [wires])
  // The outer legs, per served name: API Input to its split, its merge to
  // API Output. A block walks them too, so a request is seen leaving the
  // input and a response is seen reaching the output.
  const legs = useMemo(() => {
    const entry = new Map<string, string>()
    const exit = new Map<string, string>()
    const servedOf = new Map<string, string>()
    for (const w of wires) {
      if (!w.served) continue
      servedOf.set(w.id, w.served)
      if (w.kind === 'entry') entry.set(w.served, w.id)
      if (w.kind === 'exit') exit.set(w.served, w.id)
    }
    return { entry, exit, servedOf }
  }, [wires])
  const flows = useMemo(
    () =>
      targetFlows(routing, frame?.deployments, stale, keyFor).map((f) => ({
        ...f,
        leadIn: legs.entry.get(legs.servedOf.get(f.pathKey) ?? '') ?? null,
      })),
    [routing, frame, stale, keyFor, legs],
  )
  const streaming = useMemo(() => mostlyStreaming(streamingRatio(mix.data?.requests ?? [])), [mix.data])
  const streams = useMemo(
    () =>
      wires
        .filter((w) => w.kind === 'local-back' || w.kind === 'cloud-back')
        .map((w) => ({
          pathKey: w.id,
          tokensPerSec: stale
            ? 0
            : ((w.kind === 'local-back'
                ? frame?.deployments?.find((d) => d.deployment_id === w.target_id)?.tokens_per_sec
                : frame?.remotes?.find((r) => r.target_id === w.target_id)?.tokens_per_sec) ?? 0),
          streaming: w.served === selDep ? streaming : null,
          leadOut: w.served ? (legs.exit.get(w.served) ?? null) : null,
        })),
    [wires, frame, stale, selDep, streaming, legs],
  )
  useParticleField({ layerRef, flows, streams, paths })
  return (
    <ViewportPortal>
      <svg className="wparticles" aria-hidden="true">
        <g ref={layerRef} />
      </svg>
    </ViewportPortal>
  )
}
