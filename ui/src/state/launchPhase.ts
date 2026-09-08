/** The steps a launch goes through, and what to call them.
 *
 *  Two screens draw this ladder -- the first-run wizard's stepper and the
 *  rail's activity rows -- and before this file they drew two different ones,
 *  because the wizard inferred its phases from bytes appearing on disk and the
 *  rail had nothing to draw at all. The server reads them now
 *  (`control_plane/deploy/progress.py`), off sparkrun's output and the
 *  backend's own log, and owns the vocabulary and its order. What lives here
 *  is the half a person reads.
 *
 *  Phases only ever move forward. A log tail is a window, not a stream: a poll
 *  can land after the interesting lines scrolled out of it and come back with
 *  an older marker. The server already folds forwards-only, and `forward`
 *  below is the same rule applied again on this side, because the wizard holds
 *  its own phase across polls and a stepper that un-ticks a step reads as
 *  something going wrong.
 */

export const LAUNCH_PHASES = [
  'preparing',
  'downloading',
  'loading',
  'starting',
  'serving',
] as const

export type LaunchPhase = (typeof LAUNCH_PHASES)[number]

/** What each step is called on screen.
 *
 *  "Starting the engine" is one caption over several real activities --
 *  compiling, capturing CUDA graphs, profiling the KV cache. They are not
 *  hidden: the verbatim line beside the step says which one is running right
 *  now. A five-word caption that changes three times is harder to read than a
 *  stable one with the detail underneath it. */
export const LAUNCH_PHASE_LABEL: Record<LaunchPhase, string> = {
  preparing: 'Preparing the machine',
  downloading: 'Downloading weights',
  loading: 'Loading onto the GPU',
  starting: 'Starting the engine',
  serving: 'Serving',
}

/** Position in the ladder, or -1 for anything this build has not heard of.
 *
 *  Not an error: the server can name a phase a deployed UI predates, and the
 *  honest response is to show its sentence and tick nothing, rather than to
 *  fail rendering the row. */
export function phaseRank(phase: string | null | undefined): number {
  const i = (LAUNCH_PHASES as readonly string[]).indexOf(phase ?? '')
  return i
}

/** The caption, or '' when there is nothing honest to call it. */
export function phaseLabel(phase: string | null | undefined): string {
  return phaseRank(phase) < 0 ? '' : LAUNCH_PHASE_LABEL[phase as LaunchPhase]
}

/** Fold a new phase into the one held, forwards only.
 *
 *  An unknown phase never displaces a known one -- it has no place in the
 *  ladder, so it cannot be shown to be later. */
export function forward(
  held: string | null,
  next: string | null | undefined,
): string | null {
  if (!next) return held
  if (held === null) return phaseRank(next) < 0 ? held : next
  return phaseRank(next) > phaseRank(held) ? next : held
}
