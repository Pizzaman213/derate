import type { FitResult, PlanResponse, ServeDecision, ServeRequirement } from '../../api/types'
import type { ApiError } from '../../api/client'
import { gbytes } from '../../format'

/** Who may press Serve, in one place.
 *
 *  Plain TS and no React, because `check.mjs` bundles with
 *  `platform: 'neutral'`: `serve.check.mjs` loads this module rather than
 *  keeping a copy of it, which is the only way a verifier stays honest about
 *  the logic the screen actually runs. */

/** The permissions this launch needs, from the backend. `overrides` is the
 *  complete list when present; the older single-gate pair is the fallback for
 *  a gateway that predates it, and no gates at all is the normal case. */
export function gatesOf(serve: ServeDecision | undefined): ServeRequirement[] {
  return (
    serve?.overrides ??
    (serve?.override_required
      ? [
          {
            param: serve.override_param ?? 'allow_over_live_memory',
            reason: serve.reason,
          },
        ]
      : [])
  )
}

/** Whether the Serve button may appear.
 *
 *  Serve is the backend's call when the backend makes one. A gateway that
 *  predates `serve` omits the field entirely, and treating that absence as a
 *  refusal would red-flag every model on an older coordinator -- so fall back
 *  to the static verdict, which is exactly what such a gateway means. A
 *  refusal with nothing on offer (a static WONT_FIT) is no button at all; with
 *  gates, the button appears once every one of them has been ticked. */
export function canServeWith(
  serve: ServeDecision | undefined,
  fit: FitResult | null,
  gates: ServeRequirement[],
  granted: Record<string, boolean>,
): boolean {
  const fitPassed =
    serve == null ? fit != null && fit.verdict !== 'wont_fit' : serve.allowed === true
  const refusedOutright = !fitPassed && gates.length === 0
  return !refusedOutright && gates.every((g) => granted[g.param] === true)
}

/** What ticking a given box means, in the first person, naming the figures it
 *  waives.
 *
 *  Composed here rather than sent by the server on purpose: it is a claim the
 *  operator is making, not a message they are dismissing, and it reads that way
 *  only if it is written in their voice. The server's own sentence renders
 *  verbatim directly above it either way.
 *
 *  The default branch matters as much as the named ones. A gateway that adds a
 *  fourth gate tomorrow still gets an honest, blocking control here instead of
 *  this client silently launching past a permission it did not recognise. */
export function claimFor(
  gate: ServeRequirement,
  live: FitResult | null,
  staticCeiling: number | null,
  plan: Pick<PlanResponse['plan'], 'node_ids'>,
): string {
  if (gate.param === 'allow_over_live_memory') {
    return live
      ? `Serve anyway. I am overriding the live fit gate, which measured ` +
          `${gbytes(live.usable_per_node, 1)} GB allocatable and refused` +
          (staticCeiling != null
            ? `. The ${gbytes(staticCeiling, 1)} GB static ceiling is not reachable right now.`
            : '.')
      : `Serve anyway. There is no live memory reading, so nothing has checked ` +
          `whether this fits on the machine as it is right now — only on idle hardware.`
  }
  if (gate.param === 'allow_mixed_hardware') {
    return (
      `Pool them anyway. I am putting ${plan.node_ids.join(' and ')} in one ` +
      `deployment even though they are not alike, and every request will run ` +
      `at the speed of the slowest one.`
    )
  }
  if (gate.param === 'allow_over_memory') {
    return (
      'Pull anyway. I am overriding the memory gate, which measured what ' +
      'that machine has free and refused; the download will proceed and may ' +
      'fill its disk.'
    )
  }
  return 'Launch anyway. I have read the reason above and am overriding it.'
}

/** The override field for a 409, or null if this 409 is not an overridable one.
 *
 *  Two gates answer 409 on this screen and they publish different names. The
 *  cluster launch refuses with `live_memory_insufficient` and reopens on
 *  `allow_over_live_memory`; the pull refuses with `pull_over_memory` and
 *  reopens on `allow_over_memory`. Matching only the first left a real pull
 *  refusal rendering as a bare red line with the way past it unreachable.
 *
 *  Unrecognised codes return null deliberately: a 409 this screen has no
 *  override for is an error, and offering a checkbox that sends a field the
 *  gateway ignores would be a button that does nothing twice. */
export function refusalCode(e: ApiError): string | null {
  try {
    const parsed = JSON.parse(e.body) as { error?: { code?: unknown } }
    switch (parsed.error?.code) {
      case 'live_memory_insufficient':
        return 'allow_over_live_memory'
      case 'pull_over_memory':
        return 'allow_over_memory'
      default:
        return null
    }
  } catch {
    return null
  }
}
