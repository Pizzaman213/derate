import { quantileSorted, ascending } from "d3-array";

type Maybe = number | null | undefined;

function isNum(v: Maybe): v is number {
  return typeof v === "number" && !Number.isNaN(v);
}

/** Drop null/undefined/NaN values. */
export function clean(values: readonly Maybe[]): number[] {
  const out: number[] = [];
  for (const v of values) if (isNum(v)) out.push(v);
  return out;
}

/** Sorted copy of the finite inputs. */
function sortedClean(values: readonly Maybe[]): number[] {
  return clean(values).sort(ascending);
}

/** R type-7 quantile of an ascending-sorted array (same as d3.quantileSorted). */
export function quantile(sorted: readonly Maybe[], p: number): number {
  const s = clean(sorted);
  const q = quantileSorted(s, p);
  return q === undefined ? NaN : q;
}

/** Sum of finite values (0 for empty). */
export function sum(values: readonly Maybe[]): number {
  let s = 0;
  for (const v of values) if (isNum(v)) s += v;
  return s;
}

/** Arithmetic mean, NaN for empty input. */
export function mean(values: readonly Maybe[]): number {
  const c = clean(values);
  return c.length ? sum(c) / c.length : NaN;
}

/** Median (type 7), NaN for empty input. */
export function median(values: readonly Maybe[]): number {
  return quantile(sortedClean(values), 0.5);
}

/** Sample variance (n-1), NaN when fewer than 2 values. */
export function variance(values: readonly Maybe[]): number {
  const c = clean(values);
  const n = c.length;
  if (n < 2) return NaN;
  const m = sum(c) / n;
  let ss = 0;
  for (const v of c) ss += (v - m) * (v - m);
  return ss / (n - 1);
}

/** Sample standard deviation. */
export function stdev(values: readonly Maybe[]): number {
  return Math.sqrt(variance(values));
}

/** [min, max] of finite values, or null for empty input. */
export function extent(values: readonly Maybe[]): [number, number] | null {
  let lo = Infinity;
  let hi = -Infinity;
  for (const v of values) {
    if (!isNum(v)) continue;
    if (v < lo) lo = v;
    if (v > hi) hi = v;
  }
  return lo === Infinity ? null : [lo, hi];
}

export interface BoxStats {
  n: number;
  min: number;
  q1: number;
  median: number;
  q3: number;
  max: number;
  iqr: number;
  lowerWhisker: number;
  upperWhisker: number;
  outliers: number[];
  mean: number;
}

/** Tukey box-plot statistics; whiskers reach the most extreme points within k*IQR of the quartiles. */
export function boxStats(values: readonly Maybe[], opts: { whisker?: number } = {}): BoxStats | null {
  const s = sortedClean(values);
  const n = s.length;
  if (!n) return null;
  const k = opts.whisker ?? 1.5;
  const q1 = quantile(s, 0.25);
  const med = quantile(s, 0.5);
  const q3 = quantile(s, 0.75);
  const iqr = q3 - q1;
  const lo = q1 - k * iqr;
  const hi = q3 + k * iqr;
  const inside = s.filter((v) => v >= lo && v <= hi);
  const first = s[0] as number;
  const last = s[n - 1] as number;
  return {
    n,
    min: first,
    q1,
    median: med,
    q3,
    max: last,
    iqr,
    lowerWhisker: inside.length ? (inside[0] as number) : q1,
    upperWhisker: inside.length ? (inside[inside.length - 1] as number) : q3,
    outliers: s.filter((v) => v < lo || v > hi),
    mean: sum(s) / n,
  };
}

/** Rule-of-thumb KDE bandwidth (Silverman or Scott), always positive. */
export function bandwidth(values: readonly Maybe[], method: "silverman" | "scott" = "silverman"): number {
  const s = sortedClean(values);
  const n = s.length;
  if (n === 0) return 1;
  const sd = n > 1 ? stdev(s) : 0;
  const iqr = quantile(s, 0.75) - quantile(s, 0.25);
  let h: number;
  if (method === "scott") {
    h = 1.06 * sd * Math.pow(n, -0.2);
  } else {
    const spread = iqr > 0 ? Math.min(sd, iqr / 1.34) : sd;
    h = 0.9 * spread * Math.pow(n, -0.2);
  }
  if (!(h > 0) || !Number.isFinite(h)) {
    const m = Math.abs(sum(s) / n);
    h = m > 0 ? m * 0.1 : 1;
  }
  return h;
}

export interface KdeOptions {
  bandwidth?: number;
  kernel?: "gaussian" | "epanechnikov";
  points?: number;
  extent?: [number, number];
  cut?: number;
}

const INV_SQRT_2PI = 1 / Math.sqrt(2 * Math.PI);

/** Kernel density estimate evaluated on an evenly spaced grid. */
export function kde(values: readonly Maybe[], opts: KdeOptions = {}): { x: number; y: number }[] {
  const s = sortedClean(values);
  const n = s.length;
  if (!n) return [];
  const h = opts.bandwidth ?? bandwidth(s);
  const kernel = opts.kernel ?? "gaussian";
  const points = Math.max(2, opts.points ?? 128);
  const cut = opts.cut ?? 3;
  const [lo, hi] = opts.extent ?? [(s[0] as number) - cut * h, (s[n - 1] as number) + cut * h];
  const k =
    kernel === "gaussian"
      ? (u: number) => INV_SQRT_2PI * Math.exp(-0.5 * u * u)
      : (u: number) => (Math.abs(u) <= 1 ? 0.75 * (1 - u * u) : 0);
  const out: { x: number; y: number }[] = [];
  const step = (hi - lo) / (points - 1);
  for (let i = 0; i < points; i++) {
    const x = lo + i * step;
    let acc = 0;
    for (const v of s) acc += k((x - v) / h);
    out.push({ x, y: acc / (n * h) });
  }
  return out;
}

/** Empirical CDF as step points with ties collapsed; last p is 1. */
export function ecdf(values: readonly Maybe[]): { x: number; p: number }[] {
  const s = sortedClean(values);
  const n = s.length;
  const out: { x: number; p: number }[] = [];
  for (let i = 0; i < n; i++) {
    const v = s[i] as number;
    if (i + 1 < n && s[i + 1] === v) continue;
    out.push({ x: v, p: (i + 1) / n });
  }
  return out;
}

/** Inverse standard normal CDF (Acklam, refined by one Halley step). */
export function normalQuantile(p: number): number {
  if (Number.isNaN(p) || p < 0 || p > 1) return NaN;
  if (p === 0) return -Infinity;
  if (p === 1) return Infinity;
  const a = [-3.969683028665376e1, 2.209460984245205e2, -2.759285104469687e2, 1.38357751867269e2, -3.066479806614716e1, 2.506628277459239];
  const b = [-5.447609879822406e1, 1.615858368580409e2, -1.556989798598866e2, 6.680131188771972e1, -1.328068155288572e1];
  const c = [-7.784894002430293e-3, -3.223964580411365e-1, -2.400758277161838, -2.549732539343734, 4.374664141464968, 2.938163982698783];
  const d = [7.784695709041462e-3, 3.224671290700398e-1, 2.445134137142996, 3.754408661907416];
  const [a0, a1, a2, a3, a4, a5] = a as [number, number, number, number, number, number];
  const [b0, b1, b2, b3, b4] = b as [number, number, number, number, number];
  const [c0, c1, c2, c3, c4, c5] = c as [number, number, number, number, number, number];
  const [d0, d1, d2, d3] = d as [number, number, number, number];
  const pl = 0.02425;
  let x: number;
  if (p < pl) {
    const q = Math.sqrt(-2 * Math.log(p));
    x = (((((c0 * q + c1) * q + c2) * q + c3) * q + c4) * q + c5) / ((((d0 * q + d1) * q + d2) * q + d3) * q + 1);
  } else if (p <= 1 - pl) {
    const q = p - 0.5;
    const r = q * q;
    x = ((((((a0 * r + a1) * r + a2) * r + a3) * r + a4) * r + a5) * q) / (((((b0 * r + b1) * r + b2) * r + b3) * r + b4) * r + 1);
  } else {
    const q = Math.sqrt(-2 * Math.log(1 - p));
    x = -(((((c0 * q + c1) * q + c2) * q + c3) * q + c4) * q + c5) / ((((d0 * q + d1) * q + d2) * q + d3) * q + 1);
  }
  // Halley refinement
  const e = normalCdf(x) - p;
  const u = e * Math.sqrt(2 * Math.PI) * Math.exp((x * x) / 2);
  return x - u / (1 + (x * u) / 2);
}

/** Complementary error function: Taylor series for |x| < 2, Lentz continued fraction beyond. */
function erfc(x: number): number {
  const ax = Math.abs(x);
  let r: number;
  if (ax < 2) {
    // series for erf
    let term = ax;
    let s = ax;
    const x2 = ax * ax;
    for (let k = 1; k < 200; k++) {
      term *= -x2 / k;
      const add = term / (2 * k + 1);
      s += add;
      if (Math.abs(add) < 1e-17 * Math.abs(s)) break;
    }
    r = 1 - (2 / Math.sqrt(Math.PI)) * s;
  } else {
    // continued fraction (Lentz)
    const tiny = 1e-300;
    let f = ax;
    let C = ax;
    let D = 0;
    for (let k = 1; k < 300; k++) {
      const an = k / 2;
      D = ax + an * D;
      D = D === 0 ? tiny : 1 / D;
      C = ax + an / C;
      if (C === 0) C = tiny;
      const delta = C * D;
      f *= delta;
      if (Math.abs(delta - 1) < 1e-16) break;
    }
    r = Math.exp(-ax * ax) / (f * Math.sqrt(Math.PI));
  }
  return x >= 0 ? r : 2 - r;
}

/** Standard normal CDF. */
export function normalCdf(x: number): number {
  return 0.5 * erfc(-x / Math.SQRT2);
}

/** Normal Q-Q points using plotting positions (i - 0.5)/n. */
export function qqPoints(values: readonly Maybe[], _dist: "normal" = "normal"): { theoretical: number; sample: number }[] {
  const s = sortedClean(values);
  const n = s.length;
  return s.map((v, i) => ({ theoretical: normalQuantile((i + 0.5) / n), sample: v }));
}

/** Reference line through the sample and theoretical quartiles of a normal Q-Q plot. */
export function qqLine(values: readonly Maybe[]): { slope: number; intercept: number } | null {
  const s = sortedClean(values);
  if (s.length < 2) return null;
  const y1 = quantile(s, 0.25);
  const y3 = quantile(s, 0.75);
  const x1 = normalQuantile(0.25);
  const x3 = normalQuantile(0.75);
  const slope = (y3 - y1) / (x3 - x1);
  return { slope, intercept: y1 - slope * x1 };
}

type Pt = { x: Maybe; y: Maybe };

function cleanPoints(points: readonly Pt[]): { x: number; y: number }[] {
  const out: { x: number; y: number }[] = [];
  for (const p of points) if (isNum(p.x) && isNum(p.y)) out.push({ x: p.x, y: p.y });
  return out;
}

export interface LinearFit {
  slope: number;
  intercept: number;
  r2: number;
  r: number;
  n: number;
  predict: (x: number) => number;
}

/** Ordinary least-squares line; null when n < 2 or x has zero variance. */
export function linearRegression(points: readonly Pt[]): LinearFit | null {
  const p = cleanPoints(points);
  const n = p.length;
  if (n < 2) return null;
  let mx = 0;
  let my = 0;
  for (const q of p) {
    mx += q.x;
    my += q.y;
  }
  mx /= n;
  my /= n;
  let sxx = 0;
  let sxy = 0;
  let syy = 0;
  for (const q of p) {
    sxx += (q.x - mx) ** 2;
    syy += (q.y - my) ** 2;
    sxy += (q.x - mx) * (q.y - my);
  }
  if (sxx === 0) return null;
  const slope = sxy / sxx;
  const intercept = my - slope * mx;
  const r = syy === 0 ? 1 : sxy / Math.sqrt(sxx * syy);
  return { slope, intercept, r, r2: r * r, n, predict: (x) => intercept + slope * x };
}

/** Solve A x = b by Gaussian elimination with partial pivoting; null when singular. */
function solve(A: number[][], b: number[]): number[] | null {
  const m = b.length;
  const M = A.map((row, i) => [...row, b[i] as number]);
  for (let col = 0; col < m; col++) {
    let piv = col;
    for (let r = col + 1; r < m; r++) if (Math.abs(M[r]![col]!) > Math.abs(M[piv]![col]!)) piv = r;
    if (Math.abs(M[piv]![col]!) < 1e-12) return null;
    [M[col], M[piv]] = [M[piv]!, M[col]!];
    const pr = M[col]!;
    for (let r = col + 1; r < m; r++) {
      const row = M[r]!;
      const f = row[col]! / pr[col]!;
      for (let c = col; c <= m; c++) row[c] = row[c]! - f * pr[c]!;
    }
  }
  const x = new Array<number>(m).fill(0);
  for (let r = m - 1; r >= 0; r--) {
    const row = M[r]!;
    let s = row[m]!;
    for (let c = r + 1; c < m; c++) s -= row[c]! * x[c]!;
    x[r] = s / row[r]!;
  }
  return x;
}

export interface PolyFit {
  coefficients: number[];
  r2: number;
  predict: (x: number) => number;
}

/** Least-squares polynomial fit (coefficients in ascending powers); null when underdetermined or singular. */
export function polynomialRegression(points: readonly Pt[], degree: number): PolyFit | null {
  const p = cleanPoints(points);
  const m = degree + 1;
  if (degree < 0 || p.length < m) return null;
  const A: number[][] = Array.from({ length: m }, () => new Array<number>(m).fill(0));
  const b = new Array<number>(m).fill(0);
  for (const q of p) {
    const pw: number[] = [1];
    for (let i = 1; i < 2 * m; i++) pw.push(pw[i - 1]! * q.x);
    for (let i = 0; i < m; i++) {
      b[i] = b[i]! + pw[i]! * q.y;
      const row = A[i]!;
      for (let j = 0; j < m; j++) row[j] = row[j]! + pw[i + j]!;
    }
  }
  const coefficients = solve(A, b);
  if (!coefficients) return null;
  const predict = (x: number) => {
    let s = 0;
    for (let i = m - 1; i >= 0; i--) s = s * x + coefficients[i]!;
    return s;
  };
  const my = p.reduce((a, q) => a + q.y, 0) / p.length;
  let ssr = 0;
  let sst = 0;
  for (const q of p) {
    ssr += (q.y - predict(q.x)) ** 2;
    sst += (q.y - my) ** 2;
  }
  return { coefficients, r2: sst === 0 ? 1 : 1 - ssr / sst, predict };
}

function pairs(xs: readonly Maybe[], ys: readonly Maybe[]): [number[], number[]] {
  const a: number[] = [];
  const b: number[] = [];
  const n = Math.min(xs.length, ys.length);
  for (let i = 0; i < n; i++) {
    const x = xs[i];
    const y = ys[i];
    if (isNum(x) && isNum(y)) {
      a.push(x);
      b.push(y);
    }
  }
  return [a, b];
}

function pearsonClean(a: number[], b: number[]): number {
  const n = a.length;
  if (n < 2) return NaN;
  const ma = sum(a) / n;
  const mb = sum(b) / n;
  let sab = 0;
  let saa = 0;
  let sbb = 0;
  for (let i = 0; i < n; i++) {
    const da = a[i]! - ma;
    const db = b[i]! - mb;
    sab += da * db;
    saa += da * da;
    sbb += db * db;
  }
  return saa === 0 || sbb === 0 ? NaN : sab / Math.sqrt(saa * sbb);
}

/** Average ranks (1-based, ties share the mean rank). */
function ranks(v: number[]): number[] {
  const idx = v.map((_, i) => i).sort((i, j) => v[i]! - v[j]!);
  const r = new Array<number>(v.length);
  for (let i = 0; i < idx.length; ) {
    let j = i;
    while (j + 1 < idx.length && v[idx[j + 1]!] === v[idx[i]!]) j++;
    const avg = (i + j) / 2 + 1;
    for (let k = i; k <= j; k++) r[idx[k]!] = avg;
    i = j + 1;
  }
  return r;
}

/** Pearson correlation over pairwise-complete observations; NaN when undefined. */
export function pearson(xs: readonly Maybe[], ys: readonly Maybe[]): number {
  const [a, b] = pairs(xs, ys);
  return pearsonClean(a, b);
}

/** Spearman rank correlation over pairwise-complete observations. */
export function spearman(xs: readonly Maybe[], ys: readonly Maybe[]): number {
  const [a, b] = pairs(xs, ys);
  return pearsonClean(ranks(a), ranks(b));
}

/** Correlation matrix of named columns; undefined entries become null. */
export function correlationMatrix(
  columns: Record<string, readonly Maybe[]>,
  method: "pearson" | "spearman" = "pearson",
): { names: string[]; matrix: (number | null)[][] } {
  const names = Object.keys(columns);
  const f = method === "spearman" ? spearman : pearson;
  const matrix = names.map((a, i) =>
    names.map((b, j) => {
      if (i === j) return 1;
      const r = f(columns[a]!, columns[b]!);
      return Number.isNaN(r) ? null : r;
    }),
  );
  return { names, matrix };
}

export interface ControlLimits {
  mean: number;
  ucl: number;
  lcl: number;
  sigma: number;
  outOfControl: number[];
}

/** Shewhart control limits (XmR by default); outOfControl lists indices into the original array. */
export function controlLimits(
  values: readonly Maybe[],
  opts: { method?: "individuals" | "sigma"; sigmas?: number } = {},
): ControlLimits {
  const k = opts.sigmas ?? 3;
  const c = clean(values);
  const m = c.length ? sum(c) / c.length : NaN;
  let sigma: number;
  if ((opts.method ?? "individuals") === "individuals") {
    let mr = 0;
    for (let i = 1; i < c.length; i++) mr += Math.abs(c[i]! - c[i - 1]!);
    sigma = c.length > 1 ? mr / (c.length - 1) / 1.128 : NaN;
  } else {
    sigma = stdev(c);
  }
  const ucl = m + k * sigma;
  const lcl = m - k * sigma;
  const outOfControl: number[] = [];
  values.forEach((v, i) => {
    if (isNum(v) && (v > ucl || v < lcl)) outOfControl.push(i);
  });
  return { mean: m, ucl, lcl, sigma, outOfControl };
}

/** n jitter offsets uniformly in [-width/2, width/2]. */
export function jitter(n: number, width: number, random: () => number): number[] {
  const out: number[] = [];
  for (let i = 0; i < n; i++) out.push((random() - 0.5) * width);
  return out;
}

/** Seeded 32-bit PRNG returning floats in [0, 1). */
export function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** FNV-1a 32-bit hash of a string, for seeding mulberry32. */
export function hashSeed(s: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return h >>> 0;
}

/** Deterministic beeswarm dodge: each circle takes the smallest |offset| free of overlaps. */
export function beeswarm(
  values: readonly Maybe[],
  opts: { radius: number; scale: (v: number) => number },
): { value: number; x: number; offset: number; index: number }[] {
  const d = 2 * opts.radius;
  // place in value order but remember each value's input position; the scale is
  // monotone, so the neighbours that can collide form a sliding window of the placed list
  const order = values
    .map((v, i) => ({ v, i }))
    .filter((e): e is { v: number; i: number } => typeof e.v === 'number' && Number.isFinite(e.v))
    .sort((a, b) => a.v - b.v || a.i - b.i);
  const placed: { value: number; x: number; offset: number; index: number }[] = [];
  if (order.length > 4000) {
    // dense input: exact dodging is O(n x window); stack into columns one diameter wide
    // instead (Wilkinson dot plot): still no overlaps, O(n log n)
    const used = new Map<number, number>();
    for (const { v: value, i: index } of order) {
      const x0 = opts.scale(value);
      const col = Math.round(x0 / d);
      const k = used.get(col) ?? 0;
      used.set(col, k + 1);
      const offset = k === 0 ? 0 : (k % 2 ? 1 : -1) * Math.ceil(k / 2) * d;
      placed.push({ value, x: col * d, offset, index });
    }
    return placed;
  }
  let lo = 0;
  for (const { v: value, i: index } of order) {
    const x = opts.scale(value);
    while (lo < placed.length && Math.abs(placed[lo]!.x - x) >= d) lo++;
    const near = placed.slice(lo).filter((p) => Math.abs(p.x - x) < d);
    // candidate offsets: 0 and tangent positions above/below each neighbour
    const cands = [0];
    for (const p of near) {
      const dy = Math.sqrt(d * d - (p.x - x) ** 2);
      cands.push(p.offset + dy, p.offset - dy);
    }
    cands.sort((a, b) => Math.abs(a) - Math.abs(b) || b - a);
    let offset = 0;
    for (const c of cands) {
      if (near.every((p) => (p.x - x) ** 2 + (p.offset - c) ** 2 >= d * d - 1e-9)) {
        offset = c;
        break;
      }
    }
    placed.push({ value, x, offset, index });
  }
  return placed;
}
