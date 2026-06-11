import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { api } from "../api/client";
import { useSwarm } from "../context/SwarmProvider";
import type { GraphNodeSeed } from "../components/cortex/AgentGraph";
import AgentGraph from "../components/cortex/AgentGraph";
import CognitionStream from "../components/cortex/CognitionStream";
import MemoryCore from "../components/cortex/MemoryCore";

// ============================================================
// /brain — the operator's view into the swarm's mind.
//
// Three live organs:
//   1. AgentGraph     — the constellation of agents + who's talking now
//   2. CognitionStream— the inner monologue (thoughts / learnings / LLM)
//   3. MemoryCore     — accumulated insights + skills, breathing brighter
//
// All three are fed by the app-wide SwarmProvider (live WS) and the
// REST snapshot endpoints. Nothing here mutates state — it's a scope,
// not a console.
// ============================================================

export default function CortexBrainPage() {
  const { status, lastEventAt, counts } = useSwarm();

  // Seed the graph from the swarm snapshot; refetch keeps node states fresh.
  const snapshot = useQuery({
    queryKey: ["swarm_snapshot"],
    queryFn: api.swarmSnapshot,
    refetchInterval: 6_000,
  });

  const seeds: GraphNodeSeed[] = useMemo(() => {
    const agents = (snapshot.data?.agents as any[]) ?? [];
    return agents
      .filter((a) => a && typeof a.name === "string")
      .map((a) => ({
        id: a.name as string,
        label: a.name as string,
        state: typeof a.state === "string" ? a.state : undefined,
        capabilities: Array.isArray(a.capabilities) ? a.capabilities : undefined,
        provider: typeof a.provider === "string" ? a.provider : undefined,
        current_task:
          typeof a.current_task === "string" ? a.current_task : null,
      }));
  }, [snapshot.data]);

  const runningTasks = (snapshot.data?.running_tasks as any[]) ?? [];
  const totalEvents = useMemo(
    () => Object.values(counts).reduce((a, b) => a + b, 0),
    [counts],
  );

  return (
    <div className="space-y-5">
      <CortexKeyframes />

      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="display text-4xl">
            <span className="text-accent">Cortex</span>{" "}
            <span className="text-chrome-bright">Brain</span>
          </h1>
          <p className="text-text-secondary text-sm mt-1 max-w-xl">
            A live scope into the swarm's mind — who's talking, what it's
            thinking, and everything it has learned.
          </p>
        </div>
        <div className="flex items-center gap-3">
          <Heartbeat lastEventAt={lastEventAt} />
          <ConnBadge status={status} />
        </div>
      </header>

      {/* Top strip: live vitals */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <Vital
          label="agents"
          value={seeds.length}
          icon="fa-robot"
          sub={`${seeds.filter((s) => (s.state || "").toLowerCase() === "busy").length} busy`}
        />
        <Vital
          label="running"
          value={runningTasks.length}
          icon="fa-bolt"
          tone="amber"
          sub="tasks in flight"
        />
        <Vital
          label="events"
          value={totalEvents}
          icon="fa-wave-square"
          sub="since mount"
        />
        <Vital
          label="thoughts"
          value={counts.thought ?? 0}
          icon="fa-lightbulb"
          tone="amber"
          sub={`${counts.convo ?? 0} LLM calls`}
        />
      </div>

      {/* Main triptych */}
      <div className="grid grid-cols-1 xl:grid-cols-[minmax(0,1fr)_360px] gap-4 items-stretch">
        {/* Left column: graph on top, memory core below */}
        <div className="grid grid-rows-[minmax(360px,1fr)_minmax(0,auto)] gap-4 min-w-0">
          <section className="panel-card flex flex-col min-h-0">
            <div className="panel-header">
              <div className="min-w-0">
                <h2 className="display text-lg flex items-center gap-2">
                  <i className="fa-solid fa-diagram-project text-accent text-base not-italic" />
                  <span className="text-accent">Swarm Constellation</span>
                </h2>
                <p className="text-text-secondary text-sm mt-1">
                  Agents orbit; links pulse as messages travel between them.
                </p>
              </div>
              <span className="text-[0.6rem] font-mono uppercase tracking-wider text-text-dim shrink-0">
                {seeds.length} nodes
              </span>
            </div>
            <div className="flex-1 min-h-[320px] p-2">
              <AgentGraph seeds={seeds} className="h-full" />
            </div>
          </section>

          <section className="panel-card flex flex-col min-h-[420px] xl:min-h-[460px]">
            <MemoryCore className="h-full" />
          </section>
        </div>

        {/* Right rail: cognition stream */}
        <section className="panel-card flex flex-col min-h-[520px] xl:min-h-0 xl:max-h-none">
          <CognitionStream className="h-full" />
        </section>
      </div>
    </div>
  );
}

function Vital({
  label,
  value,
  icon,
  sub,
  tone = "accent",
}: {
  label: string;
  value: number;
  icon: string;
  sub?: string;
  tone?: "accent" | "amber";
}) {
  const color = tone === "amber" ? "text-amber" : "text-accent";
  return (
    <div className="panel-card p-3 flex flex-col gap-1 min-w-0">
      <div className="flex items-center gap-2 section-label">
        <i className={["fa-solid", icon, color, "opacity-80"].join(" ")} aria-hidden />
        <span>{label}</span>
      </div>
      <div className={["font-mono text-2xl font-medium tracking-tight tabular-nums", color].join(" ")}>
        {value}
      </div>
      {sub && <div className="text-text-dim text-[0.65rem] font-mono truncate">{sub}</div>}
    </div>
  );
}

function ConnBadge({ status }: { status: "connecting" | "open" | "closed" }) {
  const map = {
    open: {
      cls: "bg-status-green/20 text-status-green border-status-green/30",
      dot: "bg-status-green",
      label: "live",
      pulse: true,
    },
    connecting: {
      cls: "bg-status-yellow/20 text-status-yellow border-status-yellow/30",
      dot: "bg-status-yellow",
      label: "connecting",
      pulse: true,
    },
    closed: {
      cls: "bg-status-red/20 text-status-red border-status-red/30",
      dot: "bg-status-red",
      label: "offline",
      pulse: false,
    },
  } as const;
  const s = map[status];
  return (
    <span
      className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded text-[0.65rem] uppercase tracking-wider border ${s.cls}`}
      role="status"
      aria-label={`Live connection ${s.label}`}
    >
      <span className={`w-1.5 h-1.5 rounded-full ${s.dot} ${s.pulse ? "animate-pulse" : ""}`} />
      {s.label}
    </span>
  );
}

// A tiny EKG-style heartbeat that ticks each time a new event lands.
function Heartbeat({ lastEventAt }: { lastEventAt: number | null }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, []);
  const age = lastEventAt == null ? Infinity : now - lastEventAt;
  const idle = age > 15_000;
  return (
    <span
      className="hidden sm:inline-flex items-center gap-1.5 text-[0.62rem] font-mono uppercase tracking-wider text-text-dim"
      title={
        lastEventAt
          ? `Last event ${Math.round(age / 1000)}s ago`
          : "No events yet"
      }
    >
      <i
        className={[
          "fa-solid fa-heart-pulse",
          idle ? "text-text-dim" : "text-accent cortex-beat",
        ].join(" ")}
        aria-hidden
      />
      {lastEventAt == null ? "idle" : idle ? `${Math.round(age / 1000)}s` : "active"}
    </span>
  );
}

// Cortex-local keyframes. We don't own globals.css, so the graph pulse /
// node halo / heartbeat animations are injected here, scoped by class.
// All are disabled under prefers-reduced-motion.
function CortexKeyframes() {
  return (
    <style
      // eslint-disable-next-line react/no-danger
      dangerouslySetInnerHTML={{
        __html: `
.cortex-node-label { fill: #8a9bb0; font-family: "JetBrains Mono", ui-monospace, monospace; }
.cortex-node:hover .cortex-node-label, .cortex-node:focus .cortex-node-label { fill: #e4eaf0; }
.cortex-node-task { fill: #566577; font-family: "JetBrains Mono", ui-monospace, monospace; }
.cortex-node:focus { outline: none; }
.cortex-node:focus circle:nth-of-type(1) { stroke: #5ee4ff; }
.cortex-pulse {
  animation: cortex-travel 0.85s cubic-bezier(0.4, 0, 0.6, 1) forwards;
  filter: drop-shadow(0 0 4px #5ee4ff);
}
@keyframes cortex-travel {
  from { transform: translate(var(--cx-x0, 0), var(--cx-y0, 0)); opacity: 0; }
  10%  { opacity: 1; }
  90%  { opacity: 1; }
  to   {
    transform: translate(calc(var(--cx-x0, 0) + var(--cx-dx, 0)), calc(var(--cx-y0, 0) + var(--cx-dy, 0)));
    opacity: 0;
  }
}
.cortex-node-halo {
  transform-box: fill-box;
  transform-origin: center;
  animation: cortex-halo 2.2s ease-in-out infinite;
}
@keyframes cortex-halo {
  0%, 100% { opacity: 0.5; transform: scale(1); }
  50% { opacity: 0.12; transform: scale(1.12); }
}
.cortex-beat { animation: cortex-beat 1.1s ease-in-out infinite; }
@keyframes cortex-beat {
  0%, 100% { transform: scale(1); }
  20% { transform: scale(1.25); }
  40% { transform: scale(0.95); }
}
@media (prefers-reduced-motion: reduce) {
  .cortex-pulse, .cortex-node-halo, .cortex-beat { animation: none !important; }
  .cortex-pulse { opacity: 0 !important; }
}
`,
      }}
    />
  );
}
