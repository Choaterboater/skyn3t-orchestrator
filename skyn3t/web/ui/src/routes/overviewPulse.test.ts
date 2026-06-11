import { describe, it, expect } from "vitest";
import { parseEventTs, bucketEventsPerSecond } from "./OverviewPage";
import { matchSlotForEvent } from "../components/FleetGrid";
import type { FleetSlotStatus } from "../api/client";

describe("parseEventTs", () => {
  it("parses ISO strings to epoch ms", () => {
    expect(parseEventTs("2026-06-11T00:00:00.000Z")).toBe(Date.parse("2026-06-11T00:00:00.000Z"));
  });
  it("treats small numbers as epoch seconds", () => {
    expect(parseEventTs(1_700_000_000)).toBe(1_700_000_000 * 1000);
  });
  it("treats large numbers as epoch ms", () => {
    expect(parseEventTs(1_700_000_000_000)).toBe(1_700_000_000_000);
  });
  it("parses numeric strings (legacy snapshot backfill) as seconds", () => {
    expect(parseEventTs("1700000000")).toBe(1_700_000_000 * 1000);
  });
  it("returns null for junk / missing", () => {
    expect(parseEventTs(undefined)).toBeNull();
    expect(parseEventTs(null)).toBeNull();
    expect(parseEventTs("not-a-date")).toBeNull();
    expect(parseEventTs(NaN)).toBeNull();
  });
});

describe("bucketEventsPerSecond", () => {
  it("returns a flat zero series for empty input (never throws)", () => {
    const out = bucketEventsPerSecond([], { now: 10_000, buckets: 5, bucketMs: 1000 });
    expect(out).toEqual([0, 0, 0, 0, 0]);
  });
  // Use realistic epoch-ms (> 1e12) so the seconds/ms heuristic treats them as ms.
  const NOW = 1_700_000_000_000;
  it("places the newest event in the last bucket", () => {
    const out = bucketEventsPerSecond([{ ts: NOW }], { now: NOW, buckets: 5, bucketMs: 1000 });
    expect(out[out.length - 1]).toBe(1);
    expect(out.slice(0, -1).every((v) => v === 0)).toBe(true);
  });
  it("buckets events by second offset from now", () => {
    // 0s ago, 1s ago, 1s ago, 3s ago
    const out = bucketEventsPerSecond(
      [{ ts: NOW }, { ts: NOW - 1000 }, { ts: NOW - 1500 }, { ts: NOW - 3000 }],
      { now: NOW, buckets: 5, bucketMs: 1000 },
    );
    // indices: [4]=now, [3]=1s, [1]=3s
    expect(out[4]).toBe(1);
    expect(out[3]).toBe(2);
    expect(out[1]).toBe(1);
    expect(out.reduce((a, b) => a + b, 0)).toBe(4);
  });
  it("drops events outside the rolling window", () => {
    const out = bucketEventsPerSecond(
      [{ ts: NOW - 60_000 }, { ts: NOW + 5_000 }],
      { now: NOW, buckets: 10, bucketMs: 1000 },
    );
    expect(out.reduce((a, b) => a + b, 0)).toBe(0);
  });
  it("handles ISO-string timestamps", () => {
    const now = Date.parse("2026-06-11T00:00:10.000Z");
    const out = bucketEventsPerSecond([{ ts: "2026-06-11T00:00:10.000Z" }], {
      now,
      buckets: 5,
      bucketMs: 1000,
    });
    expect(out[out.length - 1]).toBe(1);
  });
});

describe("matchSlotForEvent", () => {
  const slots: FleetSlotStatus[] = [
    { slot_id: 0, state: "building", current_slug: "alpha-site" },
    { slot_id: 1, state: "building", current_slug: "beta-app", current_brief: "ResearchAgent draft" },
    { slot_id: 2, state: "idle" },
  ];

  it("returns null when no slots or no event", () => {
    expect(matchSlotForEvent(null, slots)).toBeNull();
    expect(matchSlotForEvent({ from: "x" }, [])).toBeNull();
  });
  it("matches by payload.slug against current_slug", () => {
    expect(matchSlotForEvent({ meta: { payload: { slug: "alpha-site" } } }, slots)).toBe(0);
  });
  it("matches by payload.project_slug", () => {
    expect(matchSlotForEvent({ meta: { payload: { project_slug: "beta-app" } } }, slots)).toBe(1);
  });
  it("matches by session_id when no slug", () => {
    expect(matchSlotForEvent({ meta: { session_id: "alpha-site" } }, slots)).toBe(0);
  });
  it("falls back to fuzzy slug containment", () => {
    expect(matchSlotForEvent({ meta: { payload: { slug: "alpha" } } }, slots)).toBe(0);
  });
  it("falls back to event.from matched against the brief", () => {
    expect(matchSlotForEvent({ from: "researchagent" }, slots)).toBe(1);
  });
  it("returns null when nothing matches", () => {
    expect(matchSlotForEvent({ meta: { payload: { slug: "zzz-unknown" } } }, slots)).toBeNull();
  });
});
