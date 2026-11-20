import { tokens } from '../../../../.omp/design/tokens/tokens.ts'

// uPlot draws to a canvas and so needs literal colours, but this product is
// themed entirely with custom properties that move under both
// `prefers-color-scheme` and the header's own theme switch. Terminal.tsx
// already solved this for xterm by reading the live computed style; this is
// the same trick with the subscription xterm does not need, because a chart
// redraws continuously and a terminal does not.
//
// Nothing here invents a colour. Every value is a token from tokens.css, whose
// contrast was measured there -- the band fills are the SAME token at reduced
// alpha, which is the "one hue, two strengths" rule: an average and its
// bucket maximum are one quantity in two aggregations, not two series, so they
// must never take two hues.

export interface ChartTheme {
  /** The trace. */
  ink: string
  /** Axis labels, the baseline, the cursor. */
  muted: string
  rule: string
  panel: string
  /** avg -> max, or p50 -> p99. `ink` at low alpha. */
  band: string
  /** The hairline along the top of a band. */
  bandEdge: string
  /** A stretch the archive knows it is missing, as opposed to quiet. */
  gap: string
}

// design-pack: computed-color — uPlot paints into a canvas, where var() does
// not resolve, so the light-theme values from tokens.ts are the fallback and
// alpha() derives the bands from whatever --ink resolves to at runtime.
const FALLBACK: ChartTheme = {
  ink: tokens.light.ink,
  muted: tokens.light['ink-muted'],
  rule: tokens.light.rule,
  panel: tokens.light.panel,
  band: alpha(tokens.light.ink, 0.13)!,
  bandEdge: alpha(tokens.light.ink, 0.3)!,
  gap: alpha(tokens.light.warn, 0.12)!,
}

/** `#RRGGBB` -> `rgba(...)`. Returns null for anything else, so a token that
 *  is ever changed to a `color-mix()` degrades to the fallback rather than
 *  handing uPlot a string it will paint as black. */
function alpha(hex: string, a: number): string | null {
  const m = /^#([0-9a-f]{6})$/i.exec(hex.trim())
  if (!m) return null
  const n = parseInt(m[1]!, 16)
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`
}

export function readChartTheme(): ChartTheme {
  if (typeof document === 'undefined') return FALLBACK
  const style = getComputedStyle(document.documentElement)
  const token = (name: string, fallback: string) =>
    style.getPropertyValue(name).trim() || fallback

  const ink = token('--ink', FALLBACK.ink)
  const warn = token('--warn', tokens.light.warn)
  return {
    ink,
    muted: token('--ink-muted', FALLBACK.muted),
    rule: token('--rule', FALLBACK.rule),
    panel: token('--panel', FALLBACK.panel),
    band: alpha(ink, 0.13) ?? FALLBACK.band,
    bandEdge: alpha(ink, 0.3) ?? FALLBACK.bandEdge,
    gap: alpha(warn, 0.12) ?? FALLBACK.gap,
  }
}

/** Fires whenever the resolved palette may have changed: the OS preference
 *  flipping, or Header writing `data-theme` onto the root. Both are needed --
 *  the switch has a "system" setting that REMOVES the attribute, so watching
 *  only the attribute misses the case where system is itself in force. */
export function subscribeChartTheme(onChange: () => void): () => void {
  if (typeof window === 'undefined') return () => {}
  const mq = window.matchMedia('(prefers-color-scheme: dark)')
  mq.addEventListener('change', onChange)
  const mo = new MutationObserver(onChange)
  mo.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ['data-theme'],
  })
  return () => {
    mq.removeEventListener('change', onChange)
    mo.disconnect()
  }
}
