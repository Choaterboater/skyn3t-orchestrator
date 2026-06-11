import { describe, expect, it } from "vitest";

import {
  eventSlug,
  eventTsMs,
  normalizeSwarmEvent,
  projectSubKind,
  type SwarmEvent,
} from "./SwarmProvider";

describe("normalizeSwarmEvent", () => {
  it("keeps a well-formed live event intact and normalizes ts to ISO", () => {
    const raw = {
      kind: "thought",
      ts: "2026-06-11T12:00:00.000Z",
      from: "ResearchAgent",
      to: "ArchitectAgent",
      label: "considering options",
      event_type: "AGENT_THOUGHT",
      meta: { task_id: "t1", session_id: "s1", payload: { k: 1 } },
    };
    const e = normalizeSwarmEvent(raw);
    expect(e.kind).toBe("thought");
    expect(e.ts).toBe("2026-06-11T12:00:00.000Z");
    expect(e.from).toBe("ResearchAgent");
    expect(e.to).toBe("ArchitectAgent");
    expect(e.label).toBe("considering options");
    expect(e.event_type).toBe("AGENT_THOUGHT");
    expect(e.meta?.task_id).toBe("t1");
    expect(e.meta?.session_id).toBe("s1");
    expect(e.meta?.payload).toEqual({ k: 1 });
  });

  it("converts legacy numeric epoch-seconds ts to an ISO string", () => {
    const e = normalizeSwarmEvent({
      kind: "task",
      ts: 1700000000, // seconds
      label: "x",
      event_type: "TASK_STARTED",
    });
    expect(typeof e.ts).toBe("string");
    expect(e.ts).toBe(new Date(1700000000 * 1000).toISOString());
  });

  it("converts a numeric-string epoch ts to ISO", () => {
    const e = normalizeSwarmEvent({
      kind: "task",
      ts: "1700000000",
      label: "x",
      event_type: "TASK_STARTED",
    });
    expect(e.ts).toBe(new Date(1700000000 * 1000).toISOString());
  });

  it("supplies safe defaults for a totally empty / null input without throwing", () => {
    const e = normalizeSwarmEvent(null);
    expect(e.kind).toBe("event");
    expect(e.event_type).toBe("UNKNOWN");
    expect(e.label).toBe("");
    expect(typeof e.ts).toBe("string");
    expect(Number.isNaN(new Date(e.ts).getTime())).toBe(false);
    expect(e.meta).toBeUndefined();
  });

  it("omits meta when the source has no meta, and never carries through from/to of wrong type", () => {
    const e = normalizeSwarmEvent({
      kind: "message",
      label: "hi",
      event_type: "AGENT_MESSAGE_SENT",
      from: 42, // wrong type -> dropped
    });
    expect(e.meta).toBeUndefined();
    expect(e.from).toBeUndefined();
  });

  it("falls back to now for unparseable / empty ts", () => {
    const before = Date.now();
    const e = normalizeSwarmEvent({
      kind: "rag",
      ts: "not-a-date",
      label: "x",
      event_type: "RAG_QUERY",
    });
    const after = Date.now();
    const ms = new Date(e.ts).getTime();
    expect(Number.isNaN(ms)).toBe(false);
    expect(ms).toBeGreaterThanOrEqual(before);
    expect(ms).toBeLessThanOrEqual(after);
  });

  it("coerces a non-string label to a string", () => {
    const e = normalizeSwarmEvent({
      kind: "task",
      ts: "2026-06-11T12:00:00.000Z",
      label: 123,
      event_type: "TASK_STARTED",
    });
    expect(e.label).toBe("123");
  });
});

describe("projectSubKind", () => {
  it("returns meta.payload.kind for SYSTEM_ALERT project events", () => {
    const e: SwarmEvent = {
      kind: "project",
      ts: "2026-06-11T12:00:00.000Z",
      label: "stage done",
      event_type: "SYSTEM_ALERT",
      meta: { payload: { kind: "PROJECT_STAGE_COMPLETED", slug: "demo" } },
    };
    expect(projectSubKind(e)).toBe("PROJECT_STAGE_COMPLETED");
  });

  it("falls back to event_type for project events missing payload.kind", () => {
    const e: SwarmEvent = {
      kind: "project",
      ts: "2026-06-11T12:00:00.000Z",
      label: "x",
      event_type: "SYSTEM_ALERT",
      meta: { payload: {} },
    };
    expect(projectSubKind(e)).toBe("SYSTEM_ALERT");
  });

  it("returns top-level event_type for non-project events", () => {
    const e: SwarmEvent = {
      kind: "convo",
      ts: "2026-06-11T12:00:00.000Z",
      label: "llm",
      event_type: "LLM_EXCHANGE",
      meta: { payload: { kind: "ignored" } },
    };
    expect(projectSubKind(e)).toBe("LLM_EXCHANGE");
  });
});

describe("eventSlug", () => {
  it("prefers meta.payload.project_slug", () => {
    const e: SwarmEvent = {
      kind: "project",
      ts: "x",
      label: "",
      event_type: "SYSTEM_ALERT",
      meta: { session_id: "sess", payload: { project_slug: "alpha", slug: "beta" } },
    };
    expect(eventSlug(e)).toBe("alpha");
  });

  it("falls back to meta.payload.slug", () => {
    const e: SwarmEvent = {
      kind: "project",
      ts: "x",
      label: "",
      event_type: "SYSTEM_ALERT",
      meta: { session_id: "sess", payload: { slug: "beta" } },
    };
    expect(eventSlug(e)).toBe("beta");
  });

  it("falls back to meta.session_id when no payload slug", () => {
    const e: SwarmEvent = {
      kind: "convo",
      ts: "x",
      label: "",
      event_type: "LLM_EXCHANGE",
      meta: { session_id: "sess-123", payload: {} },
    };
    expect(eventSlug(e)).toBe("sess-123");
  });

  it("returns null when nothing identifying is present", () => {
    const e: SwarmEvent = {
      kind: "thought",
      ts: "x",
      label: "",
      event_type: "AGENT_THOUGHT",
    };
    expect(eventSlug(e)).toBeNull();
  });
});

describe("eventTsMs", () => {
  it("parses an ISO string to epoch ms", () => {
    expect(eventTsMs({ ts: "2026-06-11T12:00:00.000Z" })).toBe(
      Date.parse("2026-06-11T12:00:00.000Z"),
    );
  });

  it("treats numeric epoch-seconds as ms*1000", () => {
    expect(eventTsMs({ ts: 1700000000 as unknown as string })).toBe(
      1700000000 * 1000,
    );
  });

  it("returns null for unparseable input", () => {
    expect(eventTsMs({ ts: "nope" })).toBeNull();
    expect(eventTsMs({ ts: "" })).toBeNull();
  });
});
