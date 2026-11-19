// Text measurement. In a browser: canvas measureText in the chart font, cached. Without
// a 2D canvas (jsdom, workers): a per-character estimate that errs wide, so layout
// decisions made headless are never tighter than the real ones.

import { FONT, SIZES } from './theme'

let ctx: CanvasRenderingContext2D | null | undefined
function context(): CanvasRenderingContext2D | null {
  if (ctx !== undefined) return ctx
  try {
    ctx = typeof document !== 'undefined' ? document.createElement('canvas').getContext('2d') : null
  } catch {
    ctx = null
  }
  return ctx
}

const cache = new Map<string, number>()
// widths measured before a web font arrives are the fallback font's: forget them when it does
if (typeof document !== 'undefined' && document.fonts && typeof document.fonts.addEventListener === 'function') {
  document.fonts.addEventListener('loadingdone', () => cache.clear())
}
const WIDE = /[\u1100-\u115F\u2E80-\uA4CF\uAC00-\uD7A3\uF900-\uFAFF\uFE30-\uFE4F\uFF00-\uFF60\uFFE0-\uFFE6\u{1F300}-\u{1FAFF}\u{20000}-\u{3FFFD}]/u
const NARROW = /[il.,:;'|!ftjrI\s()[\]]/

function estimate(text: string, size: number, weight: number | string): number {
  let w = 0
  for (const ch of text) w += WIDE.test(ch) ? 1.0 : NARROW.test(ch) ? 0.32 : /[A-Z0-9MW@#%&]/.test(ch) ? 0.68 : 0.58
  return w * size * (Number(weight) >= 600 ? 1.06 : 1)
}

/** Width in px of `text` at `size` px. */
export function measure(text: string, size: number = SIZES.tick, weight: number | string = 400): number {
  const key = `${size}|${weight}|${text}`
  const hit = cache.get(key)
  if (hit !== undefined) return hit
  const c = context()
  let w: number
  if (c) {
    c.font = `${weight} ${size}px ${FONT}`
    w = c.measureText(text).width
  } else {
    w = estimate(text, size, weight)
  }
  if (cache.size > 20000) cache.clear()
  cache.set(key, w)
  return w
}

/** Shorten `text` with an ellipsis so it fits `maxWidth` px. */
export function truncate(text: string, maxWidth: number, size: number = SIZES.tick, weight: number | string = 400): string {
  if (maxWidth <= 0) return ''
  if (measure(text, size, weight) <= maxWidth) return text
  const chars = [...text]
  let lo = 0, hi = chars.length
  while (lo < hi) {
    const mid = Math.ceil((lo + hi) / 2)
    if (measure(chars.slice(0, mid).join('').trimEnd() + '…', size, weight) <= maxWidth) lo = mid
    else hi = mid - 1
  }
  return lo === 0 ? (measure('…', size, weight) <= maxWidth ? '…' : '') : chars.slice(0, lo).join('').trimEnd() + '…'
}

/** Greedy word wrap into at most `maxLines` lines of `maxWidth`; the last line is truncated. */
export function wrap(text: string, maxWidth: number, maxLines: number, size: number = SIZES.tick): string[] {
  const words = text.split(/\s+/).filter(Boolean)
  const lines: string[] = []
  let cur = ''
  for (const w of words) {
    const next = cur ? `${cur} ${w}` : w
    if (measure(next, size) <= maxWidth || !cur) cur = next
    else {
      lines.push(cur)
      cur = w
    }
  }
  if (cur) lines.push(cur)
  if (lines.length > maxLines) {
    const head = lines.slice(0, maxLines - 1)
    head.push(truncate(lines.slice(maxLines - 1).join(' '), maxWidth, size))
    return head.map((l) => truncate(l, maxWidth, size))
  }
  return lines.map((l) => truncate(l, maxWidth, size))
}
