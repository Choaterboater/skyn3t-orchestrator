import { describe, expect, it } from "vitest";

import {
  buildEventTitle,
  filterBuildEvents,
  parseConsoleTs,
  summarizeStageTimeline,
} from "./BuildConsole";
import type { SwarmEvent } from "../../context/SwarmProvider";

// Minimal SwarmEvent factory — only the fields under test, cast to the
// real type so the test exercises the exact shape consumers receive.
function ev(partial: Partial<SwarmEvent> & { kind: string }): SwarmEvent {
  return {
    ts: "2026-06-11T10:00:00Z",
    label: "",
    event_type: "",
    ...partial,
  } as SwarmEvent;
}

describe("filterBuildEvents — slug / session matching (SYSTEM_ALERT gotcha)", () => {
  it("matches PROJECT events via meta.payload.project_slug (not top-level)", () => {
    // PROJECT_* rides SYSTEM_ALERT: top-level event_type is SYSTEM_ALERT,
    // kind is 'project', and the real slug lives in meta.payload.project_slug.
    const mine = ev({
      kind: "project",
      event_type: "SYSTEM_ALERT",
      label: "PackagingAgent",
      meta: {
        session_id: "sess-xyz",
        payload: { kind: "PROJECT_STAGE_COMPLETED", project_slug: "my-app" },
      },
    });
    const other = ev({
      kind: "project",
      event_type: "SYSTEM_ALERT",
      meta: { payload: { kind: "PROJECT_STAGE_STARTED", project_slug: "other-app" } },
    });

    expect(filterBuildEvents([mine, other], "my-app")).toEqual([mine]);
  });

  it("falls back to meta.payload.slug when project_slug is absent", () => {
    const e = ev({
      kind: "project",
      event_type: "SYSTEM_ALERT",
      meta: { payload: { kind: "PROJECT_CREATED", slug: "my-app" } },
    });
    expect(filterBuildEvents([e], "my-app")).toEqual([e]);
  });

  it("matches thought/convo events for the build via meta.session_id", () => {
    // Non-project events (thought, convo) carry no project slug — they are
    // associated with the build only through meta.session_id.
    const thought = ev({
      kind: "thought",
      event_type: "AGENT_THOUGHT",
      label: "Considering the data layer",
      meta: { session_id: "sess-xyz" },
    });
    const convo = ev({
      kind: "convo",
      event_type: "LLM_EXCHANGE",
      meta: { session_id: "sess-xyz", model: "claude", duration_ms: 1200 },
    });
    const strayThought = ev({
      kind: "thought",
      event_type: "AGENT_THOUGHT",
      meta: { session_id: "sess-other" },
    });

    const out = filterBuildEvents([thought, convo, strayThought], "my-app", "sess-xyz");
    expect(out).toEqual([thought, convo]);
  });

  it("matches when eventSlug resolves to session_id even without a passed sessionId", () => {
    // eventSlug falls back to meta.session_id, so a slug equal to the
    // session id should still match a sessionless caller.
    const e = ev({ kind: "thought", meta: { session_id: "sess-xyz" } });
    expect(filterBuildEvents([e], "sess-xyz")).toEqual([e]);
  });

  it("drops events that match neither slug nor session", () => {
    const e = ev({
      kind: "project",
      event_type: "SYSTEM_ALERT",
      meta: { session_id: "sess-a", payload: { project_slug: "alpha" } },
    });
    expect(filterBuildEvents([e], "beta", "sess-b")).toEqual([]);
  });

  it("is a no-op pass-through guard for empty input", () => {
    expect(filterBuildEvents([], "my-app", "sess-xyz")).toEqual([]);
    expect(filterBuildEvents(undefined as unknown as SwarmEvent[], "x")).toEqual([]);
  });
});

describe("summarizeStageTimeline", () => {
  it("derives per-stage duration from started→completed pairs", () => {
    const events: SwarmEvent[] = [
      ev({
        kind: "project",
        event_type: "SYSTEM_ALERT",
        ts: "2026-06-11T10:00:00Z",
        label: "architect",
        meta: { payload: { kind: "PROJECT_STAGE_STARTED", project_slug: "my-app", stage: "architecture" } },
      }),
      ev({
        kind: "project",
        event_type: "SYSTEM_ALERT",
        ts: "2026-06-11T10:00:05Z",
        label: "architect",
        meta: { payload: { kind: "PROJECT_STAGE_COMPLETED", project_slug: "my-app", stage: "architecture" } },
      }),
      ev({
        kind: "project",
        event_type: "SYSTEM_ALERT",
        ts: "2026-06-11T10:00:05Z",
        label: "coder",
        meta: { payload: { kind: "PROJECT_STAGE_STARTED", project_slug: "my-app", stage: "build" } },
      }),
    ];

    const summary = summarizeStageTimeline(events);
    expect(summary.stages.map((s) => s.name)).toEqual(["architecture", "build"]);
    expect(summary.stages[0].status).toBe("completed");
    expect(summary.stages[0].durationMs).toBe(5000);
    expect(summary.stages[1].status).toBe("running");
    expect(summary.stages[1].durationMs).toBeNull();
    // Per-stage durations (completed only) feed the MiniBars/Sparkline.
    expect(summary.durations).toEqual([5000]);
    expect(summary.completed).toBe(1);
    expect(summary.total).toBe(2);
  });

  it("ignores non-stage and unrelated events", () => {
    const events: SwarmEvent[] = [
      ev({ kind: "thought", event_type: "AGENT_THOUGHT", label: "thinking" }),
      ev({ kind: "convo", event_type: "LLM_EXCHANGE" }),
    ];
    const summary = summarizeStageTimeline(events);
    expect(summary.stages).toEqual([]);
    expect(summary.durations).toEqual([]);
    expect(summary.total).toBe(0);
    expect(summary.completed).toBe(0);
  });

  it("marks a stage failed when a FAILED sub-kind arrives", () => {
    const events: SwarmEvent[] = [
      ev({
        kind: "stage",
        event_type: "PIPELINE_STAGE_STARTED",
        ts: "2026-06-11T10:00:00Z",
        meta: { payload: { stage: "verify" } },
      }),
      ev({
        kind: "stage",
        event_type: "PIPELINE_STAGE_FAILED",
        ts: "2026-06-11T10:00:03Z",
        meta: { payload: { stage: "verify" } },
      }),
    ];
    const summary = summarizeStageTimeline(events);
    expect(summary.stages[0].status).toBe("failed");
    expect(summary.stages[0].durationMs).toBe(3000);
    // Failed stages still contribute a measured duration bar.
    expect(summary.durations).toEqual([3000]);
  });

  it("tolerates empty / undefined input", () => {
    expect(summarizeStageTimeline([]).stages).toEqual([]);
    expect(summarizeStageTimeline(undefined as unknown as SwarmEvent[]).stages).toEqual([]);
  });
});

describe("parseConsoleTs", () => {
  it("parses ISO strings", () => {
    expect(parseConsoleTs("2026-06-11T10:00:00Z")?.getUTCFullYear()).toBe(2026);
  });
  it("parses legacy numeric epoch-seconds", () => {
    const d = parseConsoleTs(1_700_000_000);
    expect(d).toBeInstanceOf(Date);
    expect(d!.getTime()).toBe(1_700_000_000 * 1000);
  });
  it("returns null for junk", () => {
    expect(parseConsoleTs("not-a-date")).toBeNull();
    expect(parseConsoleTs(null)).toBeNull();
    expect(parseConsoleTs(undefined)).toBeNull();
  });
});

describe("buildEventTitle", () => {
  it("prefers a meaningful label", () => {
    expect(
      buildEventTitle(ev({ kind: "thought", label: "Designing the schema", event_type: "AGENT_THOUGHT" })),
    ).toBe("Designing the schema");
  });
  it("humanizes the project sub-kind when label is empty", () => {
    expect(
      buildEventTitle(
        ev({
          kind: "project",
          event_type: "SYSTEM_ALERT",
          label: "",
          meta: { payload: { kind: "PROJECT_STAGE_COMPLETED" } },
        }),
      ),
    ).toBe("project stage completed");
  });
});
