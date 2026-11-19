// Theme: the stylesheet every chart shares, the palette (paints are CSS expressions
// over --oc-* variables, so switching theme is pure CSS), and a static resolver that
// turns a paint into a literal colour for canvas and exports.

import { CHART_TOKENS_CSS, THEME_VALUES, SEQ_STEPS, DIV_STEPS, CAT_COUNT, type ThemeName } from '../theme/tokens.generated'
import { hexToOklab, oklabToHex } from '../theme/color.mjs'
import type { Paint } from './types'

export type { ThemeName }
const v = (name: string): Paint => `var(--oc-${name})`
const px = (name: string): number => parseFloat(THEME_VALUES.light[`--oc-${name}`] ?? '0')

/** Font sizes in px, from the tokens (theme-invariant). */
export const SIZES = { tick: px('size-tick'), label: px('size-label'), title: px('size-title'), value: px('size-value') }
export const FONT = THEME_VALUES.light['--oc-font'] ?? 'sans-serif'
export const FONT_MONO = THEME_VALUES.light['--oc-font-mono'] ?? 'monospace'
export const DURATION_MS = parseFloat(THEME_VALUES.light['--oc-dur'] ?? '0')

/** cubic-bezier(x1,y1,x2,y2) from the --oc-ease token, as an easing function. */
export const ease = (() => {
  const m = (THEME_VALUES.light['--oc-ease'] ?? '').match(/cubic-bezier\(([^)]+)\)/)
  const [x1, y1, x2, y2] = m ? m[1]!.split(',').map(Number) as [number, number, number, number] : [0.25, 0.1, 0.25, 1]
  const bx = (t: number) => 3 * (1 - t) ** 2 * t * x1 + 3 * (1 - t) * t ** 2 * x2 + t ** 3
  const by = (t: number) => 3 * (1 - t) ** 2 * t * y1 + 3 * (1 - t) * t ** 2 * y2 + t ** 3
  return (x: number) => {
    let lo = 0, hi = 1
    for (let i = 0; i < 24; i++) { const mid = (lo + hi) / 2; if (bx(mid) < x) lo = mid; else hi = mid }
    return by((lo + hi) / 2)
  }
})()

export interface Palette {
  catCount: number
  /** Categorical colour i (wraps after catCount). */
  cat(i: number): Paint
  /** Sequential ramp, t in [0, 1]. */
  seq(t: number): Paint
  /** Diverging ramp, t in [0, 1]; 0.5 is the neutral midpoint. */
  div(t: number): Paint
  /** Mix two paints in OKLab; t = share of b. */
  mix(a: Paint, b: Paint, t: number): Paint
  seqStops(): Paint[]
  divStops(): Paint[]
  surface: Paint
  surfaceRaised: Paint
  ink: Paint
  inkSecondary: Paint
  muted: Paint
  grid: Paint
  axis: Paint
  ruleStrong: Paint
  track: Paint
  focus: Paint
  positive: Paint
  negative: Paint
  warning: Paint
  neutral: Paint
  highlight: Paint
}

const clamp01 = (t: number) => (Number.isFinite(t) ? Math.min(1, Math.max(0, t)) : 0)
const mix = (a: Paint, b: Paint, t: number): Paint => {
  const p = Math.round(clamp01(t) * 1000) / 10
  if (p <= 0) return a
  if (p >= 100) return b
  return `color-mix(in oklab, ${a} ${100 - p}%, ${b})`
}
const stepped = (prefix: string, steps: number) => (t: number): Paint => {
  const x = clamp01(t) * (steps - 1)
  const i = Math.min(steps - 2, Math.floor(x))
  const f = x - i
  if (f < 0.005) return v(`${prefix}-${i}`)
  if (f > 0.995) return v(`${prefix}-${i + 1}`)
  return mix(v(`${prefix}-${i}`), v(`${prefix}-${i + 1}`), f)
}

export const palette: Palette = {
  catCount: CAT_COUNT,
  cat: (i) => v(`cat-${(((Math.floor(i) % CAT_COUNT) + CAT_COUNT) % CAT_COUNT) + 1}`),
  seq: stepped('seq', SEQ_STEPS),
  div: stepped('div', DIV_STEPS),
  mix,
  seqStops: () => Array.from({ length: SEQ_STEPS }, (_, i) => v(`seq-${i}`)),
  divStops: () => Array.from({ length: DIV_STEPS }, (_, i) => v(`div-${i}`)),
  surface: v('surface'),
  surfaceRaised: v('surface-raised'),
  ink: v('ink'),
  inkSecondary: v('ink-secondary'),
  muted: v('ink-muted'),
  grid: v('grid'),
  axis: v('axis'),
  ruleStrong: v('rule-strong'),
  track: v('track'),
  focus: v('focus'),
  positive: v('positive'),
  negative: v('negative'),
  warning: v('warning'),
  neutral: v('neutral'),
  highlight: v('highlight'),
}

/** The theme in force for an element: its own/ancestor data-theme, else the media queries. */
export function effectiveTheme(el: Element | null): ThemeName {
  const t = el?.closest?.('[data-theme]')?.getAttribute('data-theme')
  if (t === 'light' || t === 'dark' || t === 'high-contrast') return t
  const mm = typeof window !== 'undefined' ? window.matchMedia : undefined
  if (mm?.('(prefers-contrast: more)').matches) return 'high-contrast'
  if (mm?.('(prefers-color-scheme: dark)').matches) return 'dark'
  return 'light'
}

const HEX = /^#[0-9a-fA-F]{3,8}$/
/** Resolve a paint expression (var(--oc-*), color-mix(in oklab, ...), hex) to a literal colour. */
export function resolvePaint(paint: Paint | undefined, theme: ThemeName): string {
  if (!paint) return 'none'
  const p = paint.trim()
  if (p === 'none' || p === 'transparent' || p === 'currentColor' || p.startsWith('url(') || HEX.test(p) || p.startsWith('rgb')) return p
  const varM = p.match(/^var\((--[a-z0-9-]+)\)$/)
  if (varM) return THEME_VALUES[theme][varM[1]!] ?? THEME_VALUES.light[varM[1]!] ?? 'none'
  const mixM = p.match(/^color-mix\(in oklab,\s*(.+?)(?:\s+([\d.]+)%)?,\s*(.+?)(?:\s+([\d.]+)%)?\)$/)
  if (mixM) {
    const a = resolvePaint(mixM[1], theme), b = resolvePaint(mixM[3], theme)
    const pa = mixM[2] !== undefined ? +mixM[2] / 100 : mixM[4] !== undefined ? 1 - +mixM[4] / 100 : 0.5
    if (!HEX.test(a) || !HEX.test(b)) return a
    const la = hexToOklab(a), lb = hexToOklab(b)
    return oklabToHex(la.map((x, i) => x * pa + lb[i]! * (1 - pa)))
  }
  return p
}

/** Eight print/colour-blind patterns: drawn in the surface colour over the mark's fill. */
export const PATTERNS: Array<{ size: number; d: string; dot?: boolean }> = [
  { size: 6, d: 'M-1,1 l2,-2 M0,6 l6,-6 M5,7 l2,-2' },
  { size: 6, d: 'M3,0 v6' },
  { size: 5, d: '', dot: true },
  { size: 6, d: 'M0,3 h6' },
  { size: 6, d: 'M-1,5 l2,2 M0,0 l6,6 M5,-1 l2,2' },
  { size: 6, d: 'M0,0 l6,6 M6,0 l-6,6' },
  { size: 6, d: 'M3,0 v6 M0,3 h6' },
  { size: 8, d: 'M0,4 l4,-4 l4,4 l-4,4 z' },
]

export const BASE_CSS = `
.oc-root{position:relative;box-sizing:border-box;width:100%;font-family:var(--oc-font);color:var(--oc-ink);overflow:hidden;line-height:1.3}
.oc-root *{box-sizing:border-box}
.oc-title{font-size:var(--oc-size-title);font-weight:600;margin:0 0 4px;overflow-wrap:anywhere}
.oc-legend{display:flex;flex-wrap:wrap;gap:2px 12px;margin:0 0 6px;padding:0;list-style:none;font-size:var(--oc-size-label);max-width:100%}
.oc-legend[hidden]{display:none}
.oc-legend li{max-width:100%;min-width:0}
.oc-legend button,.oc-legend .oc-li{all:unset;box-sizing:border-box;display:inline-flex;align-items:center;gap:6px;max-width:100%;color:var(--oc-ink-secondary);border-radius:var(--oc-radius);padding:1px 2px;font:inherit}
.oc-legend button{min-height:24px;min-width:24px}
.oc-legend button{cursor:pointer}
.oc-legend button[aria-pressed='false']{color:var(--oc-ink-muted);text-decoration:line-through}
.oc-legend button:focus-visible{outline:var(--oc-focus-width) solid var(--oc-focus);outline-offset:1px}
.oc-legend .oc-sw{width:10px;height:10px;flex:none;border-radius:2px}
.oc-legend .oc-sw-line{height:3px;width:14px}
.oc-legend .oc-sw-dash{height:0;width:14px;border-top:2px dashed;background:none!important}
.oc-legend .oc-sw-circle{border-radius:50%}
.oc-legend [aria-pressed='false'] .oc-sw{opacity:.35}
.oc-legend .oc-lt{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
.oc-legend-title{color:var(--oc-ink-muted)}
.oc-gradient{display:flex;align-items:center;gap:6px;font-size:var(--oc-size-tick);color:var(--oc-ink-secondary);min-width:0}
.oc-gradient .oc-bar{height:8px;width:120px;max-width:40%;border-radius:2px;flex:none}
.oc-gradient .oc-bar-wrap{display:inline-flex;flex-direction:column;align-items:center;width:120px;max-width:40%;flex:none}
.oc-gradient .oc-bar-wrap .oc-bar{width:100%;max-width:none}
.oc-gradient .oc-bar-mid{font-size:var(--oc-size-tick);line-height:1.1}
.oc-stage{position:relative;width:100%}
.oc-svg{display:block;overflow:hidden;font-family:var(--oc-font)}
.oc-svg:focus{outline:none}
.oc-svg:focus-visible{outline:var(--oc-focus-width) solid var(--oc-focus);outline-offset:calc(-1 * var(--oc-focus-width))}
.oc-canvas{position:absolute;left:0;top:0;pointer-events:none}
.oc-svg text{direction:ltr;unicode-bidi:isolate}
.oc-svg [data-mark]{transition:opacity var(--oc-dur-fast)}
.oc-svg .oc-dim{opacity:.22}
.oc-svg .oc-trunc{cursor:help}
.oc-focus-ring{fill:none;stroke:var(--oc-focus);stroke-width:var(--oc-focus-width);pointer-events:none}
.oc-tooltip{position:fixed;left:0;top:0;z-index:10;pointer-events:none;background:var(--oc-tooltip-bg);color:var(--oc-tooltip-fg);border:1px solid var(--oc-tooltip-border);box-shadow:var(--oc-tooltip-shadow);border-radius:var(--oc-tooltip-radius);padding:6px 8px;font-size:var(--oc-size-label);max-width:min(320px,calc(100vw - 16px));overflow-wrap:anywhere}
.oc-tooltip[hidden]{display:none}
.oc-tooltip .oc-tt-title{font-weight:600}
.oc-tooltip .oc-tt-sw{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:6px;vertical-align:baseline}
.oc-tooltip table{border-collapse:collapse;margin-top:2px}
.oc-tooltip td{padding:0 10px 0 0;vertical-align:top;overflow-wrap:normal;min-width:6ch;max-width:22ch}
.oc-tooltip td.oc-v{font-family:var(--oc-font-mono);text-align:right;padding:0;overflow-wrap:anywhere;min-width:0;max-width:24ch}
.oc-sr{position:absolute!important;width:1px!important;height:1px!important;padding:0!important;margin:-1px!important;overflow:hidden!important;clip:rect(0 0 0 0)!important;white-space:nowrap!important;border:0!important}
.oc-table-wrap{max-width:100%;overflow-x:auto}
.oc-table{width:100%;border-collapse:collapse;font-size:var(--oc-size-label);margin-top:8px}
.oc-table th,.oc-table td{text-align:left;padding:2px 8px 2px 0;border-bottom:1px solid var(--oc-grid);overflow-wrap:anywhere}
.oc-table caption{text-align:left;color:var(--oc-ink-muted);padding-bottom:4px}
.oc-note{font-size:var(--oc-size-tick);color:var(--oc-ink-muted);margin-top:4px;overflow-wrap:anywhere}
.oc-state{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px;height:100%;min-height:96px;border:1px dashed var(--oc-axis);border-radius:var(--oc-radius);color:var(--oc-ink-secondary);background:var(--oc-surface-raised);font-size:var(--oc-size-label);text-align:center;padding:12px;overflow:auto}
.oc-state strong{color:var(--oc-ink)}
.oc-state.oc-error{border-style:solid;border-color:var(--oc-negative)}
.oc-state ul{margin:4px 0 0;padding-left:18px;text-align:left;max-width:100%;overflow-wrap:anywhere}
@media (prefers-reduced-motion: reduce){.oc-root *{transition:none!important;animation:none!important}}
`

const STYLE_ID = 'oc-chart-styles'
/** Add the token + base stylesheet to a document once. */
export function injectStyles(doc: Document): void {
  if (doc.getElementById(STYLE_ID)) return
  const style = doc.createElement('style')
  style.id = STYLE_ID
  style.textContent = CHART_TOKENS_CSS + BASE_CSS
  ;(doc.head ?? doc.documentElement).appendChild(style)
}
