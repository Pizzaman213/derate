// Colour math for the theme build and its gates: sRGB <-> OKLab, WCAG contrast,
// colour-vision-deficiency simulation (Machado 2009, severity 1.0).

export const hexToRgb = (hex) => {
  const h = hex.replace('#', '')
  const full = h.length === 3 ? h.split('').map((c) => c + c).join('') : h.slice(0, 6)
  return [0, 2, 4].map((i) => parseInt(full.slice(i, i + 2), 16) / 255)
}
export const rgbToHex = (rgb) =>
  '#' + rgb.map((x) => Math.round(Math.min(1, Math.max(0, x)) * 255).toString(16).padStart(2, '0')).join('').toUpperCase()

const toLinear = (c) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4)
const toGamma = (c) => (c <= 0.0031308 ? 12.92 * c : 1.055 * c ** (1 / 2.4) - 0.055)

export function linearToOklab([r, g, b]) {
  const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b)
  const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b)
  const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b)
  return [
    0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s,
    1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s,
    0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s,
  ]
}
export function oklabToLinear([L, a, b]) {
  const l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
  const m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
  const s = (L - 0.0894841775 * a - 1.291485548 * b) ** 3
  return [
    4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
    -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
    -0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s,
  ]
}
export const hexToOklab = (hex) => linearToOklab(hexToRgb(hex).map(toLinear))
export const oklabToHex = (lab) => rgbToHex(oklabToLinear(lab).map((c) => toGamma(Math.min(1, Math.max(0, c)))))

/** Interpolate n steps between stops (evenly spaced), in OKLab. */
export function ramp(stops, n) {
  const labs = stops.map(hexToOklab)
  const out = []
  for (let i = 0; i < n; i++) {
    const t = (i / (n - 1)) * (labs.length - 1)
    const k = Math.min(labs.length - 2, Math.floor(t))
    const f = t - k
    out.push(oklabToHex(labs[k].map((v, j) => v + (labs[k + 1][j] - v) * f)))
  }
  return out
}

export function luminance(hex) {
  const [r, g, b] = hexToRgb(hex).map(toLinear)
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}
export function contrast(a, b) {
  const x = luminance(a), y = luminance(b)
  return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05)
}

const CVD = {
  protanopia: [[0.152286, 1.052583, -0.204868], [0.114503, 0.786281, 0.099216], [-0.003882, -0.048116, 1.051998]],
  deuteranopia: [[0.367322, 0.860646, -0.227968], [0.280085, 0.672501, 0.047413], [-0.01182, 0.04294, 0.968881]],
  tritanopia: [[1.255528, -0.076749, -0.178779], [-0.078411, 0.930809, 0.147602], [0.004733, 0.691367, 0.3039]],
}
export const CVD_KINDS = ['normal', ...Object.keys(CVD)]
export function simulate(hex, kind) {
  const lin = hexToRgb(hex).map(toLinear)
  if (kind === 'normal') return linearToOklab(lin)
  const m = CVD[kind]
  return linearToOklab(m.map((r) => Math.min(1, Math.max(0, r[0] * lin[0] + r[1] * lin[1] + r[2] * lin[2]))))
}
/** Smallest pairwise OKLab distance (x100) of a palette under one vision kind. */
export function minSeparation(colors, kind) {
  const labs = colors.map((c) => simulate(c, kind))
  let min = Infinity, pair = null
  for (let i = 0; i < labs.length; i++)
    for (let j = i + 1; j < labs.length; j++) {
      const d = 100 * Math.hypot(labs[i][0] - labs[j][0], labs[i][1] - labs[j][1], labs[i][2] - labs[j][2])
      if (d < min) { min = d; pair = [i + 1, j + 1] }
    }
  return { min, pair }
}
