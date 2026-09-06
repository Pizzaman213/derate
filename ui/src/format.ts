// Number formatting. Every readout declares its decimals up front, so a value
// never changes width because it crossed a rounding boundary.

const GiB = 1024 ** 3

/** Fixed-decimal string, or an em dash for a value we do not have. A missing
 *  reading is shown as missing. It is never rendered as zero. */
export function fmt(v: number | null | undefined, decimals = 0): string {
  if (v == null || !Number.isFinite(v)) return '—'
  return v.toFixed(decimals)
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

export function planShortFromDegrees(p: {
  tensor_parallel: number
  pipeline_parallel: number
  expert_parallel: number
}): string {
  const parts: string[] = []
  if (p.tensor_parallel > 1) parts.push(`TP ${p.tensor_parallel}`)
  if (p.pipeline_parallel > 1) parts.push(`PP ${p.pipeline_parallel}`)
  if (p.expert_parallel > 1) parts.push(`EP ${p.expert_parallel}`)
  return parts.length ? parts.join(' · ') : 'single node'
}

export function relativeTime(unixSeconds: number, now = Date.now() / 1000): string {
  const d = Math.max(0, Math.round(now - unixSeconds))
  if (d < 60) return `${d}s ago`
  if (d < 3600) return `${Math.round(d / 60)}m ago`
  return `${Math.round(d / 3600)}h ago`
}
