import { describe, expect, it } from "vitest";

import { normalizeSpan } from "./TracesPage";

// Regression: the page used to key off span_id/start_ts/end_ts/error, but the
// backend TraceSpan.to_dict() sends id/start_time/end_time (ISO strings) and
// puts the error in attributes.status_message. That mismatch produced undefined
// React keys (so every row used the same undefined key, selection always
// resolved to the first span) and the timestamp + error block never rendered.
describe("TracesPage normalizeSpan", () => {
  const rawA = {
    id: "span-a",
    trace_id: "trace-1",
    parent_id: null,
    name: "outer",
    start_time: "2026-06-11T12:00:00+00:00",
    end_time: "2026-06-11T12:00:01+00:00",
    duration_ms: 1000,
    status: "ok",
    attributes: {},
  };
  const rawB = {
    id: "span-b",
    trace_id: "trace-1",
    parent_id: "span-a",
    name: "inner",
    start_time: "2026-06-11T12:00:00.500+00:00",
    end_time: "2026-06-11T12:00:00.900+00:00",
    duration_ms: 400,
    status: "error",
    attributes: { status_message: "boom: downstream timed out" },
  };

  it("derives a stable, distinct key from the backend id", () => {
    const a = normalizeSpan(rawA);
    const b = normalizeSpan(rawB);
    // span_id alias + id must be populated and unique so rows don't collide
    expect(a.id).toBe("span-a");
    expect(a.span_id).toBe("span-a");
    expect(b.id).toBe("span-b");
    expect(a.id).not.toBe(b.id);
    // pre-fix the type read raw.span_id (absent) -> undefined for every row
    expect(a.id).not.toBeUndefined();
  });

  it("renders a started label from the ISO start_time", () => {
    const a = normalizeSpan(rawA);
    expect(a.startedLabel).not.toBeNull();
    // must reflect the actual span time, not be empty/NaN
    expect(a.startedLabel).toEqual(
      new Date("2026-06-11T12:00:00+00:00").toLocaleString(),
    );
    // a span with no start_time produces no label rather than "Invalid Date"
    expect(normalizeSpan({ ...rawA, start_time: undefined }).startedLabel).toBeNull();
  });

  it("pulls the error from attributes.status_message", () => {
    expect(normalizeSpan(rawB).error).toBe("boom: downstream timed out");
    // no status_message -> no error block
    expect(normalizeSpan(rawA).error).toBeNull();
  });
});
