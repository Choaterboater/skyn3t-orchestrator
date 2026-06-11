// Pure-geometry unit tests for the chart primitives. No DOM render —
// these assert the math that produces SVG path strings & dash arrays,
// which is where all the load-bearing logic lives.

import { describe, expect, it } from "vitest";
import {
  arcDashArray,
  buildPolylinePoints,
  clamp01,
} from "./Sparkline";

/** Parse "x,y x,y ..." into [number, number][] for structural assertions. */
function parsePoints(s: string): Array<[number, number]> {
  return s
    .split(" ")
    .filter(Boolean)
    .map((p) => {
      const [x, y] = p.split(",").map(Number);
      return [x, y] as [number, number];
    });
}

describe("clamp01", () => {
  it("clamps below 0 to 0", () => {
    expect(clamp01(-5)).toBe(0);
    expect(clamp01(-0.0001)).toBe(0);
  });
  it("clamps above 1 to 1", () => {
    expect(clamp01(1.5)).toBe(1);
    expect(clamp01(42)).toBe(1);
  });
  it("passes through values within range", () => {
    expect(clamp01(0)).toBe(0);
    expect(clamp01(0.5)).toBe(0.5);
    expect(clamp01(1)).toBe(1);
  });
  it("maps non-finite inputs to 0", () => {
    expect(clamp01(NaN)).toBe(0);
    expect(clamp01(Infinity)).toBe(0);
    expect(clamp01(-Infinity)).toBe(0);
    // @ts-expect-error testing runtime tolerance to bad input
    expect(clamp01(undefined)).toBe(0);
  });
});

describe("buildPolylinePoints", () => {
  it("returns a centered baseline for empty input", () => {
    const pts = parsePoints(buildPolylinePoints([], 120, 28));
    expect(pts).toHaveLength(2);
    expect(pts[0]).toEqual([0, 14]);
    expect(pts[1]).toEqual([120, 14]);
  });

  it("returns a centered baseline for a single point", () => {
    const pts = parsePoints(buildPolylinePoints([7], 100, 40));
    expect(pts).toHaveLength(2);
    expect(pts[0][1]).toBe(20);
    expect(pts[1][1]).toBe(20);
  });

  it("returns a flat centered line when all values are equal", () => {
    const pts = parsePoints(buildPolylinePoints([5, 5, 5, 5], 120, 28));
    expect(pts).toHaveLength(4);
    for (const [, y] of pts) {
      expect(y).toBe(14);
    }
  });

  it("maps one point per value with x evenly spaced across width", () => {
    const pts = parsePoints(buildPolylinePoints([0, 1, 2], 100, 50));
    expect(pts).toHaveLength(3);
    expect(pts[0][0]).toBe(0);
    expect(pts[1][0]).toBe(50);
    expect(pts[2][0]).toBe(100);
  });

  it("puts the max value near the top (small y) and min near bottom (large y)", () => {
    const pts = parsePoints(buildPolylinePoints([0, 10], 100, 100));
    const [, yMin] = pts[0]; // value 0 → bottom
    const [, yMax] = pts[1]; // value 10 → top
    expect(yMax).toBeLessThan(yMin);
  });

  it("honors explicit min/max domain", () => {
    // value 5 in domain [0..10] should land at vertical mid-ish, not extremes.
    const pts = parsePoints(buildPolylinePoints([5], 100, 100, 0, 10));
    // single point → flat center regardless, sanity check it doesn't throw
    expect(pts).toHaveLength(2);
    // multi-point with domain
    const multi = parsePoints(buildPolylinePoints([0, 5, 10], 100, 100, 0, 10));
    expect(multi[1][1]).toBeGreaterThan(multi[2][1]); // 5 lower than 10
    expect(multi[1][1]).toBeLessThan(multi[0][1]); // 5 higher than 0
  });

  it("drops NaN/Infinity entries defensively without throwing", () => {
    const raw = [1, NaN, 2, Infinity, 3];
    const pts = parsePoints(buildPolylinePoints(raw, 120, 28));
    expect(pts).toHaveLength(3); // only finite values survive
  });

  it("never throws and degrades for non-array / null input", () => {
    expect(() => buildPolylinePoints(null, 120, 28)).not.toThrow();
    expect(() => buildPolylinePoints(undefined, 120, 28)).not.toThrow();
    const pts = parsePoints(buildPolylinePoints(null, 120, 28));
    expect(pts).toHaveLength(2);
  });

  it("guards against non-positive width/height", () => {
    expect(() => buildPolylinePoints([1, 2, 3], 0, 0)).not.toThrow();
    const pts = parsePoints(buildPolylinePoints([1, 2, 3], 0, 0));
    expect(pts).toHaveLength(3);
  });

  it("keeps all y coordinates within [0, height]", () => {
    const pts = parsePoints(buildPolylinePoints([3, 9, 1, 7, 2], 200, 60));
    for (const [, y] of pts) {
      expect(y).toBeGreaterThanOrEqual(0);
      expect(y).toBeLessThanOrEqual(60);
    }
  });
});

describe("arcDashArray", () => {
  const r = 30;
  const circ = 2 * Math.PI * r;

  it("returns [0, circumference] for value 0", () => {
    const [filled, gap] = arcDashArray(0, r);
    expect(filled).toBe(0);
    expect(gap).toBeCloseTo(circ, 1);
  });

  it("returns [circumference, 0] for value 1", () => {
    const [filled, gap] = arcDashArray(1, r);
    expect(filled).toBeCloseTo(circ, 1);
    expect(gap).toBe(0);
  });

  it("splits roughly in half at value 0.5", () => {
    const [filled, gap] = arcDashArray(0.5, r);
    expect(filled).toBeCloseTo(circ / 2, 1);
    expect(gap).toBeCloseTo(circ / 2, 1);
  });

  it("clamps values above 1", () => {
    const [filled, gap] = arcDashArray(2, r);
    expect(filled).toBeCloseTo(circ, 1);
    expect(gap).toBe(0);
  });

  it("clamps values below 0", () => {
    const [filled, gap] = arcDashArray(-1, r);
    expect(filled).toBe(0);
    expect(gap).toBeCloseTo(circ, 1);
  });

  it("filled + gap always equals the circumference", () => {
    for (const v of [0, 0.1, 0.33, 0.5, 0.9, 1]) {
      const [filled, gap] = arcDashArray(v, r);
      expect(filled + gap).toBeCloseTo(circ, 1);
    }
  });

  it("handles non-finite value (NaN) as 0 progress", () => {
    const [filled] = arcDashArray(NaN, r);
    expect(filled).toBe(0);
  });

  it("handles non-positive radius without throwing or NaN", () => {
    expect(() => arcDashArray(0.5, 0)).not.toThrow();
    const [filled, gap] = arcDashArray(0.5, 0);
    expect(filled).toBe(0);
    expect(gap).toBe(0);
  });
});
