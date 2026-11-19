// Least-squares fits for regression-scatter: the line from stats.linearRegression, the
// quadratic/cubic from stats.polynomialRegression on a centred, scaled x (raw years or
// prices make the normal equations of a cubic singular in floating point), a 95%
// confidence band for the fitted mean, and the equation text.

import { stats } from '../../core'

export interface Fit {
  degree: number
  /** Coefficients in raw x, ascending powers: y = c0 + c1 x + c2 x² + c3 x³. */
  coefficients: number[]
  r2: number
  n: number
  predict(x: number): number
  /** Half-width of the 95% confidence interval for the mean at x; null when n <= degree + 1. */
  halfWidth(x: number): number | null
}

const binom = (n: number, k: number): number => {
  let r = 1
  for (let i = 1; i <= k; i++) r = (r * (n - k + i)) / i
  return r
}

/** Inverse of a small symmetric matrix by Gauss-Jordan elimination; null when singular. */
function invert(A: number[][]): number[][] | null {
  const m = A.length
  const M = A.map((row, i) => [...row, ...Array.from({ length: m }, (_, j) => (i === j ? 1 : 0))])
  for (let c = 0; c < m; c++) {
    let p = c
    for (let r = c + 1; r < m; r++) if (Math.abs(M[r]![c]!) > Math.abs(M[p]![c]!)) p = r
    if (Math.abs(M[p]![c]!) < 1e-12) return null
    ;[M[c], M[p]] = [M[p]!, M[c]!]
    const piv = M[c]![c]!
    for (let j = 0; j < 2 * m; j++) M[c]![j] = M[c]![j]! / piv
    for (let r = 0; r < m; r++) {
      if (r === c) continue
      const f = M[r]![c]!
      if (f) for (let j = 0; j < 2 * m; j++) M[r]![j] = M[r]![j]! - f * M[c]![j]!
    }
  }
  return M.map((row) => row.slice(m))
}

/** Two-sided 97.5% quantile of Student's t: exact for df 1 and 2, Cornish-Fisher beyond (error < 0.03 at df 3). */
export function t975(df: number): number {
  if (df <= 1) return 12.706204736
  if (df === 2) return 4.302652730
  const z = stats.normalQuantile(0.975)
  const z3 = z ** 3, z5 = z ** 5, z7 = z ** 7
  return z + (z3 + z) / (4 * df) + (5 * z5 + 16 * z3 + 3 * z) / (96 * df ** 2) + (3 * z7 + 19 * z5 + 17 * z3 - 15 * z) / (384 * df ** 3)
}

/** Least-squares polynomial of `degree` through finite points; null when underdetermined. */
export function fitPolynomial(points: ReadonlyArray<{ x: number; y: number }>, degree: number): Fit | null {
  const n = points.length, m = degree + 1
  if (n < m || new Set(points.map((p) => p.x)).size < m) return null
  let mx = 0
  for (const p of points) mx += p.x
  mx /= n
  let sx = 0
  for (const p of points) sx = Math.max(sx, Math.abs(p.x - mx))
  sx = sx || 1
  const u = (x: number) => (x - mx) / sx
  const up = points.map((p) => ({ x: u(p.x), y: p.y }))

  let a: number[], r2: number, predictU: (v: number) => number
  if (degree === 1) {
    const lr = stats.linearRegression(up)
    if (!lr) return null
    a = [lr.intercept, lr.slope]
    r2 = lr.r2
    predictU = lr.predict
  } else {
    const pf = stats.polynomialRegression(up, degree)
    if (!pf) return null
    a = pf.coefficients
    r2 = pf.r2
    predictU = pf.predict
  }
  // y = Σ a_k ((x - mx) / sx)^k, expanded into powers of raw x
  const coefficients = new Array<number>(m).fill(0)
  a.forEach((ak, k) => {
    for (let j = 0; j <= k; j++) coefficients[j] = coefficients[j]! + (ak / sx ** k) * binom(k, j) * (-mx) ** (k - j)
  })

  // confidence band in the scaled space: var(ŷ) = s² vᵀ (XᵀX)⁻¹ v
  const XtX = Array.from({ length: m }, (_, i) => Array.from({ length: m }, (_, j) => up.reduce((s, p) => s + p.x ** (i + j), 0)))
  const inv = n > m ? invert(XtX) : null
  const ssr = up.reduce((s, p) => s + (p.y - predictU(p.x)) ** 2, 0)
  const s2 = n > m ? ssr / (n - m) : NaN
  const t = n > m ? t975(n - m) : NaN
  return {
    degree, coefficients, r2, n,
    predict: (x) => predictU(u(x)),
    halfWidth: (x) => {
      if (!inv || !Number.isFinite(s2)) return null
      const v = Array.from({ length: m }, (_, i) => u(x) ** i)
      let q = 0
      for (let i = 0; i < m; i++) for (let j = 0; j < m; j++) q += v[i]! * inv[i]![j]! * v[j]!
      return t * Math.sqrt(Math.max(0, s2 * q))
    },
  }
}

const SUP = ['', '', '²', '³']

/** "y = 0.5x² − 1.2x + 3" with `fmtNum` for the magnitudes; negligible terms dropped. */
export function equation(coefficients: readonly number[], fmtNum: (v: number) => string): string {
  const big = Math.max(...coefficients.map(Math.abs))
  const terms: string[] = []
  for (let k = coefficients.length - 1; k >= 0; k--) {
    const c = coefficients[k]!
    if (c === 0 || Math.abs(c) < big * 1e-12) continue
    const mag = fmtNum(Math.abs(c))
    if (mag === '0') continue
    const body = k === 0 ? mag : `${mag === '1' ? '' : mag}x${SUP[k]}`
    terms.push(terms.length ? `${c < 0 ? ' − ' : ' + '}${body}` : `${c < 0 ? '−' : ''}${body}`)
  }
  return `y = ${terms.join('') || '0'}`
}
