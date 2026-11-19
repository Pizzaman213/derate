// Colour parsing, WCAG contrast and CIE76 ΔE for the tools. Standard library
// only; the same maths as design/tokens/check.mjs and design/audit.mjs.

export type Rgba = [number, number, number, number]

const NAMED: Record<string, string> = { black: '#000000', white: '#ffffff', red: '#ff0000', green: '#008000', blue: '#0000ff', gray: '#808080', grey: '#808080', yellow: '#ffff00', orange: '#ffa500', purple: '#800080', transparent: '#00000000' }

export function parseColour(input: string): Rgba | null {
  const s = input.trim().toLowerCase()
  let m: RegExpMatchArray | null
  if ((m = s.match(/^#([0-9a-f]{6})([0-9a-f]{2})?$/))) {
    const n = parseInt(m[1]!, 16)
    return [n >> 16, (n >> 8) & 255, n & 255, m[2] ? parseInt(m[2], 16) / 255 : 1]
  }
  if ((m = s.match(/^#([0-9a-f]{3})$/))) return [...m[1]!].map((c) => parseInt(c + c, 16)).concat([1]) as Rgba
  if ((m = s.match(/^rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)(?:\s*[,/]\s*([\d.]+%?))?\s*\)$/)))
    return [+m[1]!, +m[2]!, +m[3]!, m[4] == null ? 1 : m[4].endsWith('%') ? parseFloat(m[4]) / 100 : +m[4]]
  if ((m = s.match(/^hsla?\(\s*([\d.]+)(?:deg)?[\s,]+([\d.]+)%[\s,]+([\d.]+)%(?:\s*[,/]\s*([\d.]+%?))?\s*\)$/))) {
    const h = +m[1]!, sat = +m[2]! / 100, l = +m[3]! / 100
    const k = (n: number) => (n + h / 30) % 12
    const a = sat * Math.min(l, 1 - l)
    const f = (n: number) => l - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)))
    return [f(0) * 255, f(8) * 255, f(4) * 255, m[4] == null ? 1 : m[4].endsWith('%') ? parseFloat(m[4]) / 100 : +m[4]]
  }
  if (NAMED[s]) return parseColour(NAMED[s])
  return null
}

export const toHex = ([r, g, b]: Rgba): string => '#' + [r, g, b].map((c) => Math.round(c).toString(16).padStart(2, '0').toUpperCase()).join('')

export function composite(fg: Rgba, bg: Rgba): Rgba {
  const a = fg[3]
  return [fg[0] * a + bg[0] * (1 - a), fg[1] * a + bg[1] * (1 - a), fg[2] * a + bg[2] * (1 - a), 1]
}
export function luminance([r, g, b]: Rgba): number {
  const f = (c: number) => { c /= 255; return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4 }
  return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)
}
export function contrast(fg: Rgba, bg: Rgba): number {
  const l1 = luminance(fg), l2 = luminance(bg)
  return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05)
}
function toLab([r, g, b]: Rgba): [number, number, number] {
  const lin = (c: number) => { c /= 255; return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4 }
  const [R, G, B] = [lin(r), lin(g), lin(b)]
  const X = (R * 0.4124 + G * 0.3576 + B * 0.1805) / 0.95047
  const Y = R * 0.2126 + G * 0.7152 + B * 0.0722
  const Z = (R * 0.0193 + G * 0.1192 + B * 0.9505) / 1.08883
  const f = (t: number) => (t > 0.008856 ? Math.cbrt(t) : 7.787 * t + 16 / 116)
  return [116 * f(Y) - 16, 500 * (f(X) - f(Y)), 200 * (f(Y) - f(Z))]
}
export function deltaE(a: Rgba, b: Rgba): number {
  const [l1, a1, b1] = toLab(a), [l2, a2, b2] = toLab(b)
  return Math.hypot(l1 - l2, a1 - a2, b1 - b2)
}
