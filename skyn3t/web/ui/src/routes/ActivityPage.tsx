import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, getAuthToken } from "../api/client";

// Live activity stream — agents talking, tasks running, errors firing.
// One column, newest at the top, never duplicates the Studio stage view.
// Hooks into /ws/swarm (which replays a ring buffer on connect, so the
// pane fills in immediately even between bursts).
type SwarmEvent = {
  kind?: string;
  ts?: number;
  from?: string;
  to?: string;
  label?: string;
  meta?: Record<string, any>;
};

export default function ActivityPage() {
  const [events, setEvents] = useState<SwarmEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const [kindFilter, setKindFilter] = useState<string>("");
  const wsRef = useRef<WebSocket | null>(null);

  // Snapshot the running tasks + active agents so the right rail is
  // useful even before any event has arrived.
  const snapshot = useQuery({
    queryKey: ["swarm_snapshot"],
    queryFn: api.swarmSnapshot,
    refetchInterval: 6_000,
  });

  useEffect(() => {
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    const token = getAuthToken();
    const qs = token ? `?token=${encodeURIComponent(token)}` : "";
    const url = `${proto}://${window.location.host}/ws/swarm${qs}`;
    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => setConnected(true);
    ws.onclose = () => setConnected(false);
    ws.onerror = () => setConnected(false);
    ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data);
        if (msg?.type === "swarm" && msg.data) {
          setEvents((prev) => [msg.data, ...prev].slice(0, 500));
        }
      } catch {
        /* ignore malformed */
      }
    };

    return () => {
      try {
        ws.close();
      } catch {
        /* */
      }
    };
  }, []);

  const kinds = useMemo(() => {
    const s = new Set<string>();
    for (const e of events) if (e.kind) s.add(e.kind);
    return Array.from(s).sort();
  }, [events]);

  const visible = kindFilter
    ? events.filter((e) => e.kind === kindFilter)
    : events;

  return (
    <div className="space-y-6">
      <header className="flex items-baseline justify-between gap-4">
        <div>
          <h1 className="display text-4xl">
            <span className="text-accent">Activity</span>
          </h1>
          <p className="text-text-secondary text-sm mt-1">
            Live event stream from <code className="font-mono bg-bg-3 px-1 rounded">/ws/swarm</code>.
            Newest events on top.
          </p>
        </div>
        <ConnBadge connected={connected} />
      </header>

      <div className="grid grid-cols-[minmax(0,1fr)_280px] gap-5">
        <section className="min-w-0">
          <div className="flex items-center gap-2 mb-2 flex-wrap">
            <span className="text-xs text-text-secondary">Filter:</span>
            <Chip
              active={!kindFilter}
              onClick={() => setKindFilter("")}
              label={`all (${events.length})`}
            />
            {kinds.map((k) => (
              <Chip
                key={k}
                active={kindFilter === k}
                onClick={() => setKindFilter(k)}
                label={k}
              />
            ))}
          </div>
          <div className="rounded-lg border border-border bg-bg-2 min-h-[400px] max-h-[70vh] overflow-y-auto">
            {visible.length === 0 ? (
              <p className="text-text-secondary text-sm p-6 text-center">
                {connected ? "Waiting for events…" : "Disconnected from event bus."}
              </p>
            ) : (
              <ul className="divide-y divide-border">
                {visible.map((e, i) => (
                  <EventRow key={i} e={e} />
                ))}
              </ul>
            )}
          </div>
        </section>

        <aside className="space-y-4">
          <RunningTasks data={snapshot.data} />
          <AgentStates data={snapshot.data} />
        </aside>
      </div>
    </div>
  );
}

function EventRow({ e }: { e: SwarmEvent }) {
  const ts = e.ts ? new Date(e.ts * 1000) : null;
  const color = kindColor(e.kind);
  return (
    <li className="px-3 py-2 text-sm hover:bg-bg-3 min-w-0">
      <div className="flex items-baseline gap-3 min-w-0">
        <span className="text-[0.65rem] text-text-dim font-mono shrink-0">
          {ts ? ts.toLocaleTimeString() : "—"}
        </span>
        <span
          className={`text-[0.65rem] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded border shrink-0 ${color}`}
        >
          {e.kind ?? "event"}
        </span>
        <span className="font-mono text-xs text-accent shrink-0">
          {e.from ?? "?"}
        </span>
        {e.to && (
          <>
            <i className="fa-solid fa-arrow-right text-text-dim text-[0.6rem] shrink-0" />
            <span className="font-mono text-xs text-accent shrink-0">{e.to}</span>
          </>
        )}
        <span className="truncate text-text-secondary" title={e.label}>
          {e.label}
        </span>
      </div>
    </li>
  );
}

function RunningTasks({ data }: { data: any }) {
  const tasks = (data?.running_tasks as any[]) ?? [];
  return (
    <div className="rounded-lg border border-border bg-bg-2">
      <div className="px-3 py-2 text-xs uppercase tracking-wider text-text-secondary border-b border-border bg-bg-3">
        Running ({tasks.length})
      </div>
      {tasks.length === 0 ? (
        <p className="text-xs text-text-dim p-3">Nothing running right now.</p>
      ) : (
        <ul>
          {tasks.map((t) => (
            <li
              key={t.task_id}
              className="px-3 py-2 border-b border-border last:border-0"
            >
              <div className="text-sm truncate" title={t.title}>
                {t.title}
              </div>
              <div className="text-[0.65rem] font-mono text-text-dim mt-0.5 truncate">
                {t.agent ?? "?"} · {t.task_id?.slice(0, 8)}
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function AgentStates({ data }: { data: any }) {
  const agents = (data?.agents as any[]) ?? [];
  const busy = agents.filter((a) => a.state === "busy");
  return (
    <div className="rounded-lg border border-border bg-bg-2">
      <div className="px-3 py-2 text-xs uppercase tracking-wider text-text-secondary border-b border-border bg-bg-3">
        Busy ({busy.length}/{agents.length})
      </div>
      {busy.length === 0 ? (
        <p className="text-xs text-text-dim p-3">All idle.</p>
      ) : (
        <ul>
          {busy.map((a) => (
            <li
              key={a.name}
              className="px-3 py-2 border-b border-border last:border-0"
            >
              <div className="text-sm font-mono text-accent">{a.name}</div>
              {a.current_task && (
                <div
                  className="text-[0.65rem] text-text-dim mt-0.5 truncate"
                  title={a.current_task}
                >
                  {a.current_task}
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function Chip({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={[
        "text-xs px-2 py-0.5 rounded border",
        active
          ? "bg-accent-soft text-accent border-accent-line"
          : "bg-bg-2 text-text-secondary border-border hover:border-border-strong",
      ].join(" ")}
    >
      {label}
    </button>
  );
}

function ConnBadge({ connected }: { connected: boolean }) {
  const color = connected
    ? "bg-status-green/20 text-status-green border-status-green/30"
    : "bg-status-red/20 text-status-red border-status-red/30";
  return (
    <span
      className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded text-[0.65rem] uppercase tracking-wider border ${color}`}
    >
      <span
        className={`w-1.5 h-1.5 rounded-full ${
          connected ? "bg-status-green" : "bg-status-red"
        } ${connected ? "animate-pulse" : ""}`}
      />
      {connected ? "live" : "offline"}
    </span>
  );
}

function kindColor(kind: string | undefined): string {
  switch (kind) {
    case "task_completed":
    case "stage_completed":
    case "build_passed":
      return "bg-status-green/20 text-status-green border-status-green/30";
    case "task_failed":
    case "stage_failed":
    case "build_failed":
    case "error":
      return "bg-status-red/20 text-status-red border-status-red/30";
    case "task_started":
    case "stage_started":
    case "agent_message":
      return "bg-accent-soft text-accent border-accent-line";
    default:
      return "bg-bg-3 text-text-dim border-border";
  }
}
