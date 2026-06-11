import { describe, expect, it } from "vitest";

import {
  aggregateEdges,
  buildGraph,
  decayEdges,
  edgeFromEvent,
  radialLayout,
  type EdgeObservation,
  type GraphEdge,
  type GraphNodeSeed,
} from "./AgentGraph";
import type { SwarmEvent } from "../../context/SwarmProvider";

function ev(partial: Partial<SwarmEvent>): SwarmEvent {
  return {
    kind: "message",
    ts: "2026-06-11T00:00:00Z",
    label: "",
    event_type: "AGENT_MESSAGE_SENT",
    ...partial,
  };
}

describe("edgeFromEvent", () => {
  it("extracts a directed hop from a from/to event", () => {
    expect(edgeFromEvent(ev({ from: "a", to: "b" }))).toEqual({
      from: "a",
      to: "b",
    });
  });

  it("drops self-loops", () => {
    expect(edgeFromEvent(ev({ from: "a", to: "a" }))).toBeNull();
  });

  it("drops events missing an endpoint", () => {
    expect(edgeFromEvent(ev({ from: "a" }))).toBeNull();
    expect(edgeFromEvent(ev({ to: "b" }))).toBeNull();
    expect(edgeFromEvent(ev({}))).toBeNull();
  });

  it("trims whitespace endpoints", () => {
    expect(edgeFromEvent(ev({ from: " a ", to: " b " }))).toEqual({
      from: "a",
      to: "b",
    });
  });
});

describe("radialLayout", () => {
  it("returns no nodes for an empty list", () => {
    expect(radialLayout([])).toEqual([]);
  });

  it("centers a single node", () => {
    const out = radialLayout([{ id: "solo" }]);
    expect(out).toHaveLength(1);
    expect(out[0].x).toBeCloseTo(0.5, 6);
    expect(out[0].y).toBeCloseTo(0.5, 6);
  });

  it("places the first node at the top of the ring (-90deg)", () => {
    const out = radialLayout([{ id: "a" }, { id: "b" }, { id: "c" }]);
    const first = out[0];
    expect(first.x).toBeCloseTo(0.5, 5); // top of circle: cos(-90)=0
    expect(first.y).toBeLessThan(0.5); // above center
  });

  it("is deterministic regardless of input order", () => {
    const a = radialLayout([{ id: "x" }, { id: "y" }, { id: "z" }]);
    const b = radialLayout([{ id: "z" }, { id: "x" }, { id: "y" }]);
    // Same ids -> same coords (sorted internally).
    const coords = (ns: typeof a) =>
      Object.fromEntries(ns.map((n) => [n.id, [n.x, n.y]]));
    expect(coords(a)).toEqual(coords(b));
  });

  it("orders busiest nodes first, ties broken alphabetically", () => {
    const out = radialLayout(
      [{ id: "low" }, { id: "high" }, { id: "also" }],
      { high: 10, low: 1, also: 1 },
    );
    // high (weight 10) lands first slot (top).
    expect(out[0].id).toBe("high");
    // remaining tie (also=1, low=1) sorted alphabetically: also before low.
    expect(out[1].id).toBe("also");
    expect(out[2].id).toBe("low");
  });

  it("keeps all nodes within the unit box with margin", () => {
    const out = radialLayout(
      Array.from({ length: 9 }, (_, i) => ({ id: `n${i}` })),
    );
    for (const n of out) {
      expect(n.x).toBeGreaterThanOrEqual(0.1);
      expect(n.x).toBeLessThanOrEqual(0.9);
      expect(n.y).toBeGreaterThanOrEqual(0.1);
      expect(n.y).toBeLessThanOrEqual(0.9);
    }
  });
});

describe("aggregateEdges", () => {
  it("creates a new weighted edge from a fresh observation", () => {
    const out = aggregateEdges([], [{ from: "a", to: "b" }], 1);
    expect(out).toEqual([{ from: "a", to: "b", weight: 1, lastSeen: 1 }]);
  });

  it("dedupes repeated A->B into one weighted edge", () => {
    let edges: GraphEdge[] = [];
    edges = aggregateEdges(edges, [{ from: "a", to: "b" }], 1);
    edges = aggregateEdges(edges, [{ from: "a", to: "b" }], 2);
    edges = aggregateEdges(edges, [{ from: "a", to: "b" }], 3);
    expect(edges).toHaveLength(1);
    expect(edges[0].lastSeen).toBe(3);
    // weight grows but is tempered by decay each step; still > 1.
    expect(edges[0].weight).toBeGreaterThan(1);
  });

  it("treats A->B and B->A as distinct directed edges", () => {
    let edges: GraphEdge[] = [];
    edges = aggregateEdges(edges, [{ from: "a", to: "b" }], 1);
    edges = aggregateEdges(edges, [{ from: "b", to: "a" }], 2);
    expect(edges).toHaveLength(2);
  });

  it("decays prior edges not seen this round", () => {
    const prior: GraphEdge[] = [
      { from: "a", to: "b", weight: 1, lastSeen: 1 },
    ];
    const out = aggregateEdges(prior, [{ from: "c", to: "d" }], 2, 0.5);
    const ab = out.find((e) => e.from === "a")!;
    expect(ab.weight).toBeCloseTo(0.5, 6); // 1 * 0.5
    const cd = out.find((e) => e.from === "c")!;
    expect(cd.weight).toBe(1);
  });

  it("aggregates multiple observations in a single call", () => {
    const obs: EdgeObservation[] = [
      { from: "a", to: "b" },
      { from: "a", to: "b" },
      { from: "a", to: "c" },
    ];
    const out = aggregateEdges([], obs, 5);
    const ab = out.find((e) => e.to === "b")!;
    const ac = out.find((e) => e.to === "c")!;
    expect(ab.weight).toBe(2);
    expect(ac.weight).toBe(1);
  });
});

describe("decayEdges", () => {
  it("drops edges below the weight floor", () => {
    const edges: GraphEdge[] = [
      { from: "a", to: "b", weight: 0.04, lastSeen: 10 },
      { from: "c", to: "d", weight: 0.5, lastSeen: 10 },
    ];
    const out = decayEdges(edges, 10, 0.08, 80);
    expect(out.map((e) => e.from)).toEqual(["c"]);
  });

  it("drops stale edges older than maxAge", () => {
    const edges: GraphEdge[] = [
      { from: "a", to: "b", weight: 1, lastSeen: 1 },
      { from: "c", to: "d", weight: 1, lastSeen: 90 },
    ];
    const out = decayEdges(edges, 100, 0.08, 20);
    expect(out.map((e) => e.from)).toEqual(["c"]); // a is 99 ticks stale
  });

  it("keeps fresh, heavy edges", () => {
    const edges: GraphEdge[] = [
      { from: "a", to: "b", weight: 5, lastSeen: 100 },
    ];
    expect(decayEdges(edges, 100)).toHaveLength(1);
  });
});

describe("buildGraph", () => {
  const seeds: GraphNodeSeed[] = [
    { id: "alpha", state: "busy" },
    { id: "beta", state: "idle" },
  ];

  it("positions every seed even with no traffic", () => {
    const g = buildGraph(seeds, []);
    expect(g.nodes.map((n) => n.id).sort()).toEqual(["alpha", "beta"]);
    for (const n of g.nodes) expect(n.weight).toBe(0);
  });

  it("materializes nodes for edge endpoints not in seeds", () => {
    const edges: GraphEdge[] = [
      { from: "alpha", to: "ghost", weight: 2, lastSeen: 1 },
    ];
    const g = buildGraph(seeds, edges);
    const ids = g.nodes.map((n) => n.id).sort();
    expect(ids).toContain("ghost");
    expect(ids).toContain("alpha");
    expect(ids).toContain("beta");
  });

  it("derives node weight from incident edge weights", () => {
    const edges: GraphEdge[] = [
      { from: "alpha", to: "beta", weight: 3, lastSeen: 1 },
      { from: "beta", to: "alpha", weight: 2, lastSeen: 2 },
    ];
    const g = buildGraph(seeds, edges);
    const byId = Object.fromEntries(g.nodes.map((n) => [n.id, n]));
    expect(byId.alpha.weight).toBe(5); // 3 out + 2 in
    expect(byId.beta.weight).toBe(5);
  });

  it("preserves seed metadata on positioned nodes", () => {
    const g = buildGraph(seeds, []);
    const alpha = g.nodes.find((n) => n.id === "alpha")!;
    expect(alpha.state).toBe("busy");
  });

  it("passes edges through unchanged", () => {
    const edges: GraphEdge[] = [
      { from: "alpha", to: "beta", weight: 1, lastSeen: 1 },
    ];
    const g = buildGraph(seeds, edges);
    expect(g.edges).toBe(edges);
  });
});
