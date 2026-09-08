// Number formatting. Every readout declares its decimals up front, so a value
// never changes width because it crossed a rounding boundary.

const GiB = 1024 ** 3

/** Fixed-decimal string, or an em dash for a value we do not have. A missing
 *  reading is shown as missing. It is never rendered as zero. */
export function fmt(v: number | null | undefined, decimals = 0): string {
  if (v == null || !Number.isFinite(v)) return '—'
  return v.toFixed(decimals)
}

/** `fmt` plus its unit, dropped together -- a missing reading must not render
 *  as an em dash still wearing a unit it was never measured in ("— GB/s").
 *
 *  Lifted here from the two inspectors that each had their own copy: a third
 *  caller made it a convention rather than a local helper, and three copies of
 *  a rule is how one of them quietly stops following it. */
export function fmtUnit(
  v: number | null | undefined,
  decimals: number,
  unit: string,
): string {
  const s = fmt(v, decimals)
  return s === '—' ? s : `${s} ${unit}`
}

/** Bytes to GB, binary, matching the contract's use of 1024**3 throughout. */
export function gbytes(bytes: number | null | undefined, decimals = 1): string {
  if (bytes == null || !Number.isFinite(bytes)) return '—'
  return (bytes / GiB).toFixed(decimals)
}

export function gbNum(bytes: number): number {
  return bytes / GiB
}

/** Rounds a percentage for use inside a sentence (an aria-label or a title),
 *  where `fmt`'s em dash would read strangely. A non-finite input never
 *  leaks into copy as "NaN percent" or "Infinity percent". */
export function pct(v: number | null | undefined): string {
  return typeof v === 'number' && Number.isFinite(v) ? String(Math.round(v)) : '—'
}

/** "GB10", "RTX 3090" — the marketing prefix wastes width in a 200px column. */
export function shortGpu(name: string): string {
  return name.replace(/^NVIDIA\s+/i, '').replace(/^GeForce\s+/i, '')
}

/** What to call a machine that has no GPU name to show.
 *
 *  Every surface that prints hardware does `shortGpu(gpu_name) || device_class`
 *  and so printed the wire value raw — which meant a Raspberry Pi's row read
 *  "unknown", the same word the roster used for a DGX Spark whose container
 *  was started without `--gpus`. The probe now tells those two apart
 *  (`DeviceClass.CPU` vs `UNKNOWN`), and this is where that distinction has to
 *  survive into the sentence the operator actually reads: "CPU only" is a
 *  description of the machine, "unidentified" is an admission about the probe.
 *
 *  An unrecognised value passes through as itself rather than becoming
 *  "unidentified": a class this build predates is not the same as one the
 *  coordinator could not read, and inventing the stronger claim would hide a
 *  version skew behind a hardware fault. */
export function deviceClassLabel(cls: string | undefined): string {
  switch (cls) {
    case 'gb10':
      return 'GB10'
    case 'discrete':
      return 'discrete GPU'
    case 'apple':
      return 'Apple silicon'
    case 'cpu':
      return 'CPU only'
    case 'unknown':
      return 'unidentified'
    default:
      return cls ?? ''
  }
}

/** The plan caption, spelled exactly as the server spells it.
 *
 *  A port of `control_plane/gateway/internal_api.py::_plan_label`, and it had
 *  drifted twice over: it never read `data_parallel`, and it joined with ' · '
 *  where the server joins with ' + '. The cluster floor plates render the
 *  server's own string (TopologyDeployment.plan) while every other screen
 *  rendered this one, so one deployment with DP > 1 read as two different
 *  plans depending which screen you were on -- against a project rule that
 *  planner strings are the product and are rendered verbatim.
 *
 *  `ui/src/api/contracts.check.mjs` runs the Python over a matrix of degrees
 *  and diffs it against this function, so the port cannot drift again in
 *  silence. Prefer a caption the server sent; this is for the call sites that
 *  hold degrees and no string. */
export function planShortFromDegrees(p: {
  tensor_parallel: number
  pipeline_parallel: number
  expert_parallel: number
  data_parallel?: number
}): string {
  const parts: string[] = []
  if (p.tensor_parallel > 1) parts.push(`TP ${p.tensor_parallel}`)
  if (p.pipeline_parallel > 1) parts.push(`PP ${p.pipeline_parallel}`)
  if (p.expert_parallel > 1) parts.push(`EP ${p.expert_parallel}`)
  if ((p.data_parallel ?? 1) > 1) parts.push(`DP ${p.data_parallel}`)
  return parts.length ? parts.join(' + ') : 'single node'
}

export function relativeTime(unixSeconds: number, now = Date.now() / 1000): string {
  const d = Math.max(0, Math.round(now - unixSeconds))
  if (d < 60) return `${d}s ago`
  if (d < 3600) return `${Math.round(d / 60)}m ago`
  return `${Math.round(d / 3600)}h ago`
}

/** A remaining time that something else measured, worded for a caption.
 *
 *  Deliberately vague at the top end and honest at the bottom: the estimates
 *  this renders come from tqdm, which extrapolates from throughput so far, so
 *  "3m 47s" would be spurious precision on a number that moves every second.
 *  Rounded to minutes above a minute, and "under a minute" below one rather
 *  than a countdown of seconds that will be wrong before it is read.
 *
 *  null in, null out: no estimate is not an estimate of zero, and every caller
 *  is expected to say nothing rather than say "0s left". */
export function remainingLabel(seconds: number | null | undefined): string | null {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return null
  if (seconds < 60) return 'under a minute left'
  const mins = Math.round(seconds / 60)
  if (mins < 60) return `about ${mins} min left`
  const hours = Math.floor(mins / 60)
  const rest = mins % 60
  return rest === 0 ? `about ${hours} h left` : `about ${hours} h ${rest} min left`
}

/** A byte count at a scale that shows it.
 *
 *  `gbytes` is right for weights and wrong for everything under a gigabyte: a
 *  1.6 MB metadata stub rendered as "0.0 GiB" reads as an empty measurement
 *  rather than a small one, and the model cache is full of them.
 */
export function sizeLabel(bytes: number): string {
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)} GiB`
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(bytes >= 10 * 1024 ** 2 ? 0 : 1)} MiB`
  return `${Math.max(1, Math.round(bytes / 1024))} KiB`
}
