/** The variant table's pure derivations, out here so they can be checked.
 *
 *  `QuantLadder.tsx` is a component module: `rows.check.mjs` bundles plain TS
 *  and cannot reach into it. These two functions are where the table decides
 *  what a verdict LOOKS like, which is exactly the judgement that wants a
 *  verifier -- the wire is asserted elsewhere, and a green `tsc` says nothing
 *  about either of them.
 */
import type { QuantVariant } from '../../api/types'

export interface Lamp {
  signal: 'live' | 'warn' | 'fault' | 'idle'
  label: string
}

/** The Fit column's lamp.
 *
 *  Every verdict field on a row is the fit gate's answer about the machines in
 *  `sized_on`: `_variant_verdicts` filters to profiles with addressable memory
 *  before it walks, so a provider's box is not merely unaccounted for, it was
 *  excluded. Under a runtime that serves from that box the lamp would be
 *  reporting a different machine -- so it reports nothing, which is what the
 *  hollow state is for.
 */
export function fitLamp(variant: QuantVariant, onCluster = true): Lamp {
  if (!onCluster) return { signal: 'idle', label: 'not judged here' }
  switch (variant.verdict) {
    case 'fits':
      return { signal: 'live', label: 'fits' }
    case 'fits_degraded':
      return { signal: 'warn', label: 'loads, but decode is slow' }
    case 'wont_fit':
      // Two different situations drew identically here. The gate refuses
      // either way -- the signal stays `fault`, the live verdict governs, and
      // the Serve button stays shut -- but a row the hardware HOLDS, blocked
      // only by what is resident this second, wants somebody to go and look at
      // the machine rather than give up on the model.
      return variant.static_fits === true
        ? { signal: 'fault', label: 'will not fit right now — fits on an idle machine' }
        : { signal: 'fault', label: 'will not fit' }
    default:
      // A launchable row with no measured size was never handed to the gate,
      // and "not checked" is the wrong word for that: it reads as a spinner
      // that has not landed yet, when the truth is that nothing is coming.
      // The gateway refuses to judge these rather than pricing them from the
      // dtype formula -- on `Qwen/Qwen3.8-Flash-Next` that formula claimed
      // 54.7 GiB to spare on a checkpoint that overflows by 4.1.
      return unsized(variant)
        ? { signal: 'idle', label: 'cannot be sized' }
        : { signal: 'idle', label: 'not checked' }
  }
}

/** A row the fit gate deliberately did not judge.
 *
 *  Mirrors `capacity_api.py::_unsized`. A GGUF row is never launchable and is
 *  priced from its own measured files, so this is only ever true of a
 *  safetensors repository whose weight index could not be read -- gated,
 *  rate-limited, or publishing no index at all.
 */
export function unsized(variant: QuantVariant): boolean {
  return variant.launchable && variant.file_bytes == null
}

/** The Decode cell.
 *
 *  The gateway sends `predicted_decode_tps` on every row it judged, refusals
 *  included -- it is a real property of the shape and the bandwidth, and the
 *  wire does not decide what a table prints. The screen does, and a throughput
 *  is about a model that loaded: printed under a refusal it reads as a promise
 *  about something that is not going to run.
 *
 *  Gated on `fits`, not on `verdict === 'fits'`, and the difference is the
 *  point: `fits` is true for `fits_degraded` too, so the one row whose whole
 *  reason for existing IS its decode figure keeps it.
 */
export function decodeLabel(variant: QuantVariant): string {
  return variant.predicted_decode_tps != null && variant.fits === true
    ? `${variant.predicted_decode_tps.toFixed(0)} tok/s`
    : '—'
}
