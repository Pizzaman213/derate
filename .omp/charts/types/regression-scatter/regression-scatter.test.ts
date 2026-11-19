import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import regressionScatter, { emptiestCorner } from './index'
import { equation, fitPolynomial, t975 } from './fit'

chartSuite({
  module: regressionScatter,
  dir: import.meta.dirname,
  role: 'point',
  marks: { basic: 10, realistic: 32, 'edge-single': 1, 'edge-duplicates': 20, 'edge-nulls': 5, 'edge-large': 5201, 'edge-zeros': 10 },
  summary: /^Regression scatter plot, 10 points, x from 1 to 10, y from 3\.2 to 21\.3, strong positive correlation \(r = 1\), linear fit y = 2x \+ 1\.05, R² = 0\.999$/,
  malformed: [
    { name: 'a text value', data: [{ x: 1, y: 2 }, { x: 'one', y: 3 }], message: /field 'x' must be a number, got 'one' at row 2/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
    { name: 'a mapping to a missing column', data: [{ a: 1, b: 2 }], options: { fields: { x: 'a', y: 'bb' } }, message: /column 'bb' not found/ },
  ],
})

const line = (f: (x: number) => number, xs: number[]) => xs.map((x) => ({ x, y: f(x) }))

describe('regression-scatter: math', () => {
  it('recovers a known straight line exactly', () => {
    const fit = fitPolynomial(line((x) => 2 * x + 1, [0, 1, 2, 3, 4, 5]), 1)!
    expect(fit.coefficients[0]).toBeCloseTo(1, 10)
    expect(fit.coefficients[1]).toBeCloseTo(2, 10)
    expect(fit.r2).toBeCloseTo(1, 12)
  })
  it('least squares on noisy data matches the closed form', () => {
    // x = 1..5, y = 2, 4, 5, 4, 5: slope 0.6, intercept 2.2, R² 0.6
    const fit = fitPolynomial([{ x: 1, y: 2 }, { x: 2, y: 4 }, { x: 3, y: 5 }, { x: 4, y: 4 }, { x: 5, y: 5 }], 1)!
    expect(fit.coefficients[1]).toBeCloseTo(0.6, 10)
    expect(fit.coefficients[0]).toBeCloseTo(2.2, 10)
    expect(fit.r2).toBeCloseTo(0.6, 10)
  })
  it('recovers quadratic and cubic coefficients, even at year-sized x', () => {
    const q = fitPolynomial(line((x) => x * x - 3 * x + 2, [-2, -1, 0, 1, 2, 3, 4]), 2)!
    expect(q.coefficients.map((c) => Math.round(c * 1e6) / 1e6)).toEqual([2, -3, 1])
    const c = fitPolynomial(line((x) => 0.5 * x ** 3 - x + 4, [-3, -2, -1, 0, 1, 2, 3, 4]), 3)!
    expect(c.coefficients[3]).toBeCloseTo(0.5, 8)
    expect(c.coefficients[2]).toBeCloseTo(0, 8)
    expect(c.coefficients[1]).toBeCloseTo(-1, 8)
    expect(c.coefficients[0]).toBeCloseTo(4, 8)
    const years = fitPolynomial(line((x) => 0.01 * (x - 2000) ** 2 + 5, [1990, 1995, 2000, 2005, 2010, 2015, 2020]), 2)!
    expect(years.r2).toBeCloseTo(1, 8)
    expect(years.predict(2010)).toBeCloseTo(6, 6)
  })
  it('returns null when the degree is underdetermined', () => {
    expect(fitPolynomial([{ x: 1, y: 1 }, { x: 2, y: 2 }], 2)).toBeNull()
    expect(fitPolynomial([{ x: 1, y: 1 }, { x: 1, y: 2 }], 1)).toBeNull()
  })
  it('confidence band: t quantiles and the textbook half-width', () => {
    expect(t975(1)).toBeCloseTo(12.706, 3)
    expect(t975(2)).toBeCloseTo(4.303, 3)
    expect(t975(10)).toBeCloseTo(2.228, 2)
    expect(t975(1000)).toBeCloseTo(1.962, 2)
    // x = 1..5, y = 2,4,5,4,5: s² = 2.4/3, Sxx = 10, at x̄ = 3 the half-width is t(3)·s·sqrt(1/5)
    const fit = fitPolynomial([{ x: 1, y: 2 }, { x: 2, y: 4 }, { x: 3, y: 5 }, { x: 4, y: 4 }, { x: 5, y: 5 }], 1)!
    expect(fit.halfWidth(3)).toBeCloseTo(t975(3) * Math.sqrt(0.8 / 5), 8)
    expect(fit.halfWidth(5)).toBeCloseTo(t975(3) * Math.sqrt(0.8 * (1 / 5 + 4 / 10)), 8)
  })
  it('writes the equation with signs and superscripts', () => {
    const n = (v: number) => String(Math.round(v * 100) / 100)
    expect(equation([1, 2], n)).toBe('y = 2x + 1')
    expect(equation([2, -3, 1], n)).toBe('y = x² − 3x + 2')
    expect(equation([4, -1, 0, -0.5], n)).toBe('y = −0.5x³ − x + 4')
    expect(equation([0, 0], n)).toBe('y = 0')
  })
})

describe('regression-scatter: layout', () => {
  it('puts the equation in the emptiest corner', () => {
    const plot = { x: 0, y: 0, width: 200, height: 100 }
    // points crowd the top-left; the fit runs top-left to bottom-right
    const pts: Array<[number, number]> = [[10, 10], [20, 12], [30, 15]]
    const curve: Array<[number, number]> = Array.from({ length: 11 }, (_, i) => [i * 20, i * 10])
    expect(emptiestCorner(plot, 60, 30, pts, curve)).toEqual({ x: 134, y: 6 })
  })
  it('draws the fit as a data mark and the equation box inside the plot at 320px', () => {
    const el = host(320, 240)
    const h = regressionScatter.render(el, Array.from({ length: 12 }, (_, i) => ({ x: i, y: 0.02 * i ** 3 - 0.3 * i * i + i + 2 })), { animate: false, degree: 3 })
    const fit = el.querySelector('[data-role="fit"]')!
    expect(fit).not.toBeNull()
    const box = el.querySelector('[data-key="rect:eq:box"]')!
    const x = Number(box.getAttribute('x')), w = Number(box.getAttribute('width'))
    expect(x + w).toBeLessThanOrEqual(320)
    const text = [...el.querySelectorAll('.oc-labels text')].map((t) => t.textContent).join(' | ')
    expect(text).toMatch(/^y = 0\.02x³ − 0\.3x² \+ x \+ 2 \| R² = 1 · n = 12$/)
    h.destroy()
  })
  it('notes when there are too few points for the degree', () => {
    const el = host()
    const h = regressionScatter.render(el, [{ x: 1, y: 1 }, { x: 2, y: 3 }], { animate: false, degree: 2 })
    expect(h.markCount()).toBe(2)
    expect(el.querySelector('.oc-note')!.textContent).toBe('A quadratic fit needs at least 3 distinct x values')
    h.destroy()
  })
})
