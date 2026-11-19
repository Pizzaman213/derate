import { describe, it, expect } from "vitest";
import { quantileSorted } from "d3-array";
import {
  quantile, mean, median, variance, stdev, sum, extent, boxStats, bandwidth, kde, ecdf,
  normalQuantile, normalCdf, qqPoints, qqLine, linearRegression, polynomialRegression,
  pearson, spearman, correlationMatrix, controlLimits, jitter, mulberry32, hashSeed, beeswarm,
} from "./stats";

const ax = [10, 8, 13, 9, 11, 14, 6, 4, 12, 7, 5];
const ay = [8.04, 6.95, 7.58, 8.81, 8.33, 9.96, 7.24, 4.26, 10.84, 4.82, 5.68];

function trapezoid(pts: { x: number; y: number }[]): number {
  let a = 0;
  for (let i = 1; i < pts.length; i++) a += ((pts[i]!.y + pts[i - 1]!.y) / 2) * (pts[i]!.x - pts[i - 1]!.x);
  return a;
}

function normalSample(n: number, seed: number): number[] {
  const r = mulberry32(seed);
  return Array.from({ length: n }, () => normalQuantile(Math.min(1 - 1e-9, Math.max(1e-9, r()))));
}

describe("basic summaries", () => {
  it("ignore null/undefined/NaN", () => {
    const v = [1, null, 2, undefined, NaN, 3, 4];
    expect(sum(v)).toBe(10);
    expect(mean(v)).toBe(2.5);
    expect(median(v)).toBe(2.5);
    expect(variance(v)).toBeCloseTo(5 / 3, 12);
    expect(stdev(v)).toBeCloseTo(Math.sqrt(5 / 3), 12);
    expect(extent(v)).toEqual([1, 4]);
    expect(extent([null, NaN])).toBeNull();
    expect(mean([])).toBeNaN();
  });
  it("quantile matches d3.quantileSorted", () => {
    const s = [1, 2, 3, 4, 5, 6, 7, 8, 9, 100];
    for (const p of [0, 0.1, 0.25, 0.33, 0.5, 0.75, 0.9, 1]) expect(quantile(s, p)).toBe(quantileSorted(s, p));
    expect(quantile(s, 0.25)).toBe(3.25);
  });
});

describe("boxStats", () => {
  it("1..9 plus 100 (type 7)", () => {
    const b = boxStats([1, 2, 3, 4, 5, 6, 7, 8, 9, 100])!;
    expect(b.q1).toBe(3.25);
    expect(b.median).toBe(5.5);
    expect(b.q3).toBe(7.75);
    expect(b.iqr).toBe(4.5);
    expect(b.outliers).toEqual([100]);
    expect(b.lowerWhisker).toBe(1);
    expect(b.upperWhisker).toBe(9);
    expect(b.min).toBe(1);
    expect(b.max).toBe(100);
    expect(b.n).toBe(10);
    expect(b.mean).toBe(14.5);
  });
  it("1..9 alone: q1 3, median 5, q3 7", () => {
    const b = boxStats([1, 2, 3, 4, 5, 6, 7, 8, 9])!;
    expect([b.q1, b.median, b.q3]).toEqual([3, 5, 7]);
    expect(b.outliers).toEqual([]);
  });
  it("null on empty", () => expect(boxStats([null])).toBeNull());
});

describe("bandwidth & kde", () => {
  it("silverman formula", () => {
    const v = [1, 2, 3, 4, 5];
    const sd = Math.sqrt(2.5);
    const iqr = 2;
    expect(bandwidth(v)).toBeCloseTo(0.9 * Math.min(sd, iqr / 1.34) * Math.pow(5, -0.2), 12);
    expect(bandwidth(v, "scott")).toBeCloseTo(1.06 * sd * Math.pow(5, -0.2), 12);
  });
  it("zero spread fallback positive", () => {
    expect(bandwidth([5, 5, 5])).toBeCloseTo(0.5, 12);
    expect(bandwidth([0, 0])).toBe(1);
  });
  it("gaussian integrates to ~1", () => {
    const d = kde(normalSample(300, 7));
    expect(d).toHaveLength(128);
    expect(Math.abs(trapezoid(d) - 1)).toBeLessThan(0.01);
  });
  it("epanechnikov integrates to ~1", () => {
    const d = kde(normalSample(300, 11), { kernel: "epanechnikov", points: 256 });
    expect(Math.abs(trapezoid(d) - 1)).toBeLessThan(0.02);
  });
  it("single point gaussian peak", () => {
    const d = kde([0], { bandwidth: 1, extent: [-1, 1], points: 3 });
    expect(d[1]!.y).toBeCloseTo(1 / Math.sqrt(2 * Math.PI), 12);
    expect(d[0]!.x).toBe(-1);
  });
});

describe("ecdf", () => {
  it("collapses ties, last p = 1", () => {
    expect(ecdf([3, 1, 2, 2, null])).toEqual([
      { x: 1, p: 0.25 },
      { x: 2, p: 0.75 },
      { x: 3, p: 1 },
    ]);
  });
});

describe("normal", () => {
  it("known quantiles", () => {
    expect(Math.abs(normalQuantile(0.975) - 1.959963985)).toBeLessThan(1e-8);
    expect(Math.abs(normalQuantile(0.5))).toBeLessThan(1e-12);
    expect(Math.abs(normalQuantile(0.05) + 1.644853627)).toBeLessThan(1e-8);
    expect(Math.abs(normalQuantile(0.001) + 3.090232306)).toBeLessThan(1e-8);
    expect(Math.abs(normalQuantile(0.99) - 2.326347874)).toBeLessThan(1e-8);
  });
  it("cdf known values", () => {
    expect(normalCdf(0)).toBeCloseTo(0.5, 14);
    expect(normalCdf(1.959963985)).toBeCloseTo(0.975, 9);
    expect(normalCdf(-3)).toBeCloseTo(0.0013498980316301, 12);
    expect(normalCdf(1)).toBeCloseTo(0.841344746068543, 12);
  });
  it("qq points and line", () => {
    const q = qqPoints([3, 1, 2]);
    expect(q.map((p) => p.sample)).toEqual([1, 2, 3]);
    expect(q[0]!.theoretical).toBeCloseTo(normalQuantile(1 / 6), 12);
    expect(q[1]!.theoretical).toBeCloseTo(0, 12);
    const l = qqLine([1, 2, 3, 4, 5])!;
    expect(l.slope).toBeCloseTo(2 / (2 * normalQuantile(0.75)), 12);
    expect(l.intercept).toBeCloseTo(3, 12);
  });
});

describe("regression", () => {
  it("exact line", () => {
    const f = linearRegression([0, 1, 2, 3].map((x) => ({ x, y: 2 * x + 1 })))!;
    expect(f.slope).toBeCloseTo(2, 12);
    expect(f.intercept).toBeCloseTo(1, 12);
    expect(f.r2).toBeCloseTo(1, 12);
    expect(f.predict(10)).toBeCloseTo(21, 12);
  });
  it("Anscombe I", () => {
    const f = linearRegression(ax.map((x, i) => ({ x, y: ay[i]! })))!;
    expect(f.slope).toBeCloseTo(0.5001, 3);
    expect(f.intercept).toBeCloseTo(3.0001, 3);
    expect(f.r2).toBeCloseTo(0.6665, 3);
    expect(f.n).toBe(11);
  });
  it("degenerate -> null", () => {
    expect(linearRegression([{ x: 1, y: 1 }])).toBeNull();
    expect(linearRegression([{ x: 1, y: 1 }, { x: 1, y: 2 }])).toBeNull();
  });
  it("polynomial recovers quadratic", () => {
    const f = polynomialRegression([-2, -1, 0, 1, 2, 3].map((x) => ({ x, y: 1 - 2 * x + 3 * x * x })), 2)!;
    expect(f.coefficients[0]).toBeCloseTo(1, 9);
    expect(f.coefficients[1]).toBeCloseTo(-2, 9);
    expect(f.coefficients[2]).toBeCloseTo(3, 9);
    expect(f.r2).toBeCloseTo(1, 9);
    expect(f.predict(4)).toBeCloseTo(41, 8);
  });
  it("polynomial degree 1 equals linear", () => {
    const pts = ax.map((x, i) => ({ x, y: ay[i]! }));
    const f = polynomialRegression(pts, 1)!;
    expect(f.coefficients[1]).toBeCloseTo(linearRegression(pts)!.slope, 10);
  });
});

describe("correlation", () => {
  it("pearson Anscombe I", () => expect(pearson(ax, ay)).toBeCloseTo(0.8164, 4));
  it("pairwise complete", () => expect(pearson([1, 2, null, 3], [2, 4, 100, 6])).toBeCloseTo(1, 12));
  it("spearman monotone & ties", () => {
    expect(spearman([1, 2, 3, 4], [1, 8, 27, 64])).toBeCloseTo(1, 12);
    expect(spearman([1, 2, 3], [3, 2, 1])).toBeCloseTo(-1, 12);
  });
  it("matrix", () => {
    const m = correlationMatrix({ a: [1, 2, 3], b: [3, 2, 1], c: [5, 5, 5] });
    expect(m.names).toEqual(["a", "b", "c"]);
    expect(m.matrix[0]![0]).toBe(1);
    expect(m.matrix[0]![1]).toBeCloseTo(-1, 12);
    expect(m.matrix[0]![2]).toBeNull();
    expect(m.matrix[2]![2]).toBe(1);
  });
});

describe("controlLimits", () => {
  it("XmR by hand", () => {
    const v = [5, 7, 6, 8, 6, 20];
    // mean = 52/6; moving ranges 2,1,2,2,14 -> mean 21/5 = 4.2; sigma = 4.2/1.128
    const m = 52 / 6;
    const s = 4.2 / 1.128;
    const c = controlLimits(v);
    expect(c.mean).toBeCloseTo(m, 12);
    expect(c.sigma).toBeCloseTo(s, 12);
    expect(c.ucl).toBeCloseTo(m + 3 * s, 12);
    expect(c.lcl).toBeCloseTo(m - 3 * s, 12);
    // ucl = 8.6667 + 3 * 3.7234 = 19.837 < 20
    expect(c.outOfControl).toEqual([5]);
    const c2 = controlLimits([10, 10, 11, 10, 11, 10, null, 30]);
    // mean 83/7, MR 0,1,1,1,1,20 -> 4, sigma 4/1.128 = 3.546, ucl = 11.857+10.638 = 22.495
    expect(c2.outOfControl).toEqual([7]);
  });
  it("sigma method", () => {
    const c = controlLimits([1, 2, 3, 4, 5], { method: "sigma", sigmas: 2 });
    expect(c.sigma).toBeCloseTo(Math.sqrt(2.5), 12);
    expect(c.ucl).toBeCloseTo(3 + 2 * Math.sqrt(2.5), 12);
  });
});

describe("random & layout", () => {
  it("mulberry32 deterministic", () => {
    const a = mulberry32(42);
    const b = mulberry32(42);
    const xs = Array.from({ length: 5 }, () => a());
    expect(xs).toEqual(Array.from({ length: 5 }, () => b()));
    expect(xs.every((x) => x >= 0 && x < 1)).toBe(true);
    expect(mulberry32(43)()).not.toBe(xs[0]);
  });
  it("hashSeed stable", () => {
    expect(hashSeed("")).toBe(0x811c9dc5);
    expect(hashSeed("a")).toBe(0xe40c292c);
    expect(hashSeed("ab")).not.toBe(hashSeed("ba"));
  });
  it("jitter bounds", () => {
    const j = jitter(100, 4, mulberry32(1));
    expect(j).toHaveLength(100);
    expect(j.every((o) => o >= -2 && o <= 2)).toBe(true);
    expect(jitter(2, 10, () => 0)).toEqual([-5, -5]);
  });
  it("beeswarm no overlaps", () => {
    const r = mulberry32(5);
    const vals = Array.from({ length: 200 }, () => r() * 100);
    const radius = 3;
    const out = beeswarm(vals, { radius, scale: (v) => v * 5 });
    expect(out).toHaveLength(200);
    for (let i = 0; i < out.length; i++)
      for (let j = i + 1; j < out.length; j++) {
        const d = Math.hypot(out[i]!.x - out[j]!.x, out[i]!.offset - out[j]!.offset);
        expect(d).toBeGreaterThanOrEqual(2 * radius - 1e-9);
      }
    expect(beeswarm([1, 1], { radius: 1, scale: (v) => v }).map((p) => p.offset)).toEqual([0, 2]);
  });
});
