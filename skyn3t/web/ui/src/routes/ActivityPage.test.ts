import { describe, expect, it } from "vitest";

import {
  activityDetail,
  activityHeadline,
  dedupeActivityEvents,
  filterActivityEvents,
  isLowSignalEvent,
} from "./ActivityPage";

describe("ActivityPage helpers", () => {
  it("filters low-signal convo events from useful view", () => {
    const events = [
      { kind: "convo", from: "llm", label: "llm · gpt-4.1", event_type: "LLM_EXCHANGE" },
      { kind: "project", from: "studio", label: "PackagingAgent", event_type: "PROJECT_STAGE_COMPLETED" },
    ];

    expect(filterActivityEvents(events, "useful")).toEqual([events[1]]);
    expect(filterActivityEvents(events, "all")).toEqual(events);
    expect(isLowSignalEvent(events[0])).toBe(true);
  });

  it("dedupes adjacent identical events", () => {
    const events = [
      { kind: "task", from: "code_agent", label: "building file 1/20", event_type: "TASK_STARTED" },
      { kind: "task", from: "code_agent", label: "building file 1/20", event_type: "TASK_STARTED" },
      { kind: "task", from: "code_agent", label: "building file 2/20", event_type: "TASK_STARTED" },
    ];

    expect(dedupeActivityEvents(events)).toEqual([events[0], events[2]]);
  });

  it("builds readable headline and detail text", () => {
    const event = {
      kind: "project",
      from: "studio",
      to: "PackagingAgent",
      label: "packaging_agent",
      event_type: "PROJECT_STAGE_COMPLETED",
    };

    expect(activityHeadline(event)).toBe("packaging_agent");
    expect(activityDetail(event)).toBe("studio · → PackagingAgent · project stage completed");
  });
});
