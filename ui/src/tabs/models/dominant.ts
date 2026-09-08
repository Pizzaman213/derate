/** The accent colour taken from the avatar itself.
 *
 *  A card tinted with the publisher's own logo colour is the thing that makes
 *  Unsloth's grid read as a catalogue of products rather than a table with
 *  pictures. Reimplemented from that behaviour; their implementation is
 *  AGPL-3.0-only and is not the source of any line here.
 *
 *  The avatar is served by the coordinator itself now (`owner.ts`), so the
 *  canvas read is same-origin and cannot be tainted at all -- it used to
 *  depend on `cdn-avatars.huggingface.co` sending
 *  `access-control-allow-origin: *`, which was true but was somebody else's
 *  header to change. The guard stays anyway: when a read throws, or the image
 *  never arrives, this returns null and the card keeps its hashed palette
 *  colour. Never a blank card and never a thrown error. */

const cache = new Map<string, string | null>()
const inflight = new Set<string>()
const listeners = new Set<() => void>()

/** Small enough that the whole read is a few hundred pixels. Averaging a
 *  512px logo would cost real time on a grid and give the same answer. */
const SAMPLE = 16

function notify(): void {
  for (const fn of listeners) fn()
}

export function subscribeDominant(fn: () => void): () => void {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

/** The most saturated colour the image actually contains, or null.
 *
 *  Weighted by saturation rather than a plain mean: averaging every pixel of a
 *  logo on a white field returns a pale grey, which is the one colour that
 *  carries no identity at all. Near-white and near-black pixels are dropped for
 *  the same reason -- they are the background, not the brand. */
export function dominantColor(url: string | null): string | null {
  if (!url) return null
  if (cache.has(url)) return cache.get(url) ?? null
  if (inflight.has(url)) return null
  inflight.add(url)

  const img = new Image()
  img.crossOrigin = 'anonymous'
  img.decoding = 'async'
  img.onload = () => {
    let result: string | null = null
    try {
      const canvas = document.createElement('canvas')
      canvas.width = SAMPLE
      canvas.height = SAMPLE
      const ctx = canvas.getContext('2d', { willReadFrequently: true })
      if (ctx) {
        ctx.drawImage(img, 0, 0, SAMPLE, SAMPLE)
        result = pick(ctx.getImageData(0, 0, SAMPLE, SAMPLE).data)
      }
    } catch {
      // Tainted canvas, or a decode the browser will not hand back. The hashed
      // palette colour is a perfectly good accent; this is an enhancement.
      result = null
    }
    cache.set(url, result)
    inflight.delete(url)
    notify()
  }
  img.onerror = () => {
    cache.set(url, null)
    inflight.delete(url)
    notify()
  }
  img.src = url
  return null
}

function pick(data: Uint8ClampedArray): string | null {
  let r = 0
  let g = 0
  let b = 0
  let weight = 0
  for (let i = 0; i < data.length; i += 4) {
    const alpha = data[i + 3]!
    if (alpha < 128) continue
    const pr = data[i]!
    const pg = data[i + 1]!
    const pb = data[i + 2]!
    const max = Math.max(pr, pg, pb)
    const min = Math.min(pr, pg, pb)
    if (max < 24 || min > 232) continue
    const saturation = max === 0 ? 0 : (max - min) / max
    if (saturation < 0.12) continue
    const w = saturation * saturation
    r += pr * w
    g += pg * w
    b += pb * w
    weight += w
  }
  if (weight === 0) return null
  return `rgb(${Math.round(r / weight)}, ${Math.round(g / weight)}, ${Math.round(b / weight)})`
}
