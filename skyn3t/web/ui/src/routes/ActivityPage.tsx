import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, getAuthToken } from "../api/client";

// Live activity stream — agents talking, tasks running, errors firing.
// One column, newest at the top, never duplicates the Studio stage view.
// Hooks into /ws/swarm (which replays a ring buffer on connect, so the
// pane fills in immediately even between bursts).
type SwarmEvent = {
  kind?: string;
  // backend emits ISO string; legacy clients may have numeric epoch seconds
  ts?: string | number;
  from?: string;
  to?: string;
  label?: string;
  event_type?: string;
  meta?: Record<string, any>;
};

const LOW_SIGNAL_KINDS = new Set(["convo"]);

export default function ActivityPage() {
  const [events, setEvents] = useState<SwarmEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const [kindFilter, setKindFilter] = useState<string>("useful");
  const wsRef = useRef<WebSocket | null>(null);

  // Snapshot the running tasks + active agents so the right rail is
  // useful even before any event has arrived.
  const snapshot = useQuery({
    queryKey: ["swarm_snapshot"],
    queryFn: api.swarmSnapshot,
    refetchInterval: 6_000,
  });

  useEffect(() => {
    // Reconnecting WebSocket with backoff. The trick is StrictMode in
    // dev: it mounts → unmounts → re-mounts to flush effects. The
    // first WS's `onclose` was firing AFTER cleanup ran and was
    // scheduling reconnects past unmount. Solution: detach handlers
    // explicitly on cleanup and check `cancelled` inside every
    // callback.
    let cancelled = false;
    let retry = 0;
    let timer: number | null = null;

    const connect = () => {
      if (cancelled) return;
      const proto = window.location.protocol === "https:" ? "wss" : "ws";
      const token = getAuthToken();
      const qs = token ? `?token=${encodeURIComponent(token)}` : "";
      const url = `${proto}://${window.location.host}/ws/swarm${qs}`;
      let ws: WebSocket;
      try {
        ws = new WebSocket(url);
      } catch {
        scheduleReconnect();
        return;
      }
      wsRef.current = ws;

      ws.onopen = () => {
        if (cancelled) {
          ws.close();
          return;
        }
        retry = 0;
        setConnected(true);
      };
      ws.onclose = () => {
        if (cancelled) return;
        setConnected(false);
        scheduleReconnect();
      };
      ws.onerror = () => {
        if (cancelled) return;
        setConnected(false);
      };
      ws.onmessage = (e) => {
        if (cancelled) return;
        try {
          const msg = JSON.parse(e.data);
          if (msg?.type === "swarm" && msg.data) {
            setEvents((prev) => [msg.data, ...prev].slice(0, 500));
          }
        } catch {
          /* ignore malformed */
        }
      };
    };

    const scheduleReconnect = () => {
      if (cancelled) return;
      // 2s, 4s, 8s, capped at 30s. Starts higher than 1s so StrictMode
      // double-mounts don't trigger an immediate second connect.
      const delay = Math.min(30000, 2000 * 2 ** retry);
      retry += 1;
      timer = window.setTimeout(connect, delay);
    };

    connect();

    return () => {
      cancelled = true;
      if (timer !== null) {
        window.clearTimeout(timer);
        timer = null;
      }
      const ws = wsRef.current;
      if (ws) {
        // Detach handlers before close so the dying socket doesn't
        // fire onclose → scheduleReconnect after we've unmounted.
        ws.onopen = null;
        ws.onclose = null;
        ws.onerror = null;
        ws.onmessage = null;
        try {
          ws.close();
        } catch {
          /* */
        }
        wsRef.current = null;
      }
    };
  }, []);

  const dedupedEvents = useMemo(() => dedupeActivityEvents(events), [events]);

  const kinds = useMemo(() => {
    const s = new Set<string>();
    for (const e of dedupedEvents) if (e.kind) s.add(e.kind);
    return Array.from(s).sort();
  }, [dedupedEvents]);

  const usefulCount = useMemo(
    () => filterActivityEvents(dedupedEvents, "useful").length,
    [dedupedEvents],
  );
  const visible = useMemo(
    () => filterActivityEvents(dedupedEvents, kindFilter),
    [dedupedEvents, kindFilter],
  );

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
              active={kindFilter === "useful"}
              onClick={() => setKindFilter("useful")}
              label={`useful (${usefulCount})`}
            />
            <Chip
              active={kindFilter === "all"}
              onClick={() => setKindFilter("all")}
              label={`all (${dedupedEvents.length})`}
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
  const ts = parseEventTs(e.ts);
  const color = kindColor(e.kind, e.event_type);
  const headline = activityHeadline(e);
  const detail = activityDetail(e);
  return (
    <li className="px-3 py-2 text-sm hover:bg-bg-3 min-w-0">
      <div className="flex items-center gap-3 min-w-0">
        <span className="text-[0.65rem] text-text-dim font-mono shrink-0">
          {ts ? ts.toLocaleTimeString() : "—"}
        </span>
        <span
          className={`text-[0.65rem] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded border shrink-0 ${color}`}
        >
          {e.kind ?? "event"}
        </span>
      </div>
      <div className="mt-1 text-sm text-text-primary break-words" title={headline}>
        {headline}
      </div>
      {detail && (
        <div className="mt-1 text-[0.7rem] font-mono text-text-dim break-words">
          {detail}
        </div>
      )}
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

function parseEventTs(ts: unknown): Date | null {
  if (ts == null) return null;
  if (typeof ts === "number") {
    // legacy epoch-seconds; multiply to ms
    const d = new Date(ts * 1000);
    return isNaN(d.getTime()) ? null : d;
  }
  if (typeof ts === "string") {
    const d = new Date(ts);
    return isNaN(d.getTime()) ? null : d;
  }
  return null;
}

export function isLowSignalEvent(e: SwarmEvent): boolean {
  return LOW_SIGNAL_KINDS.has(String(e.kind || "").toLowerCase());
}

export function dedupeActivityEvents(events: SwarmEvent[]): SwarmEvent[] {
  const out: SwarmEvent[] = [];
  let lastKey = "";
  for (const event of events) {
    const key = [
      event.kind ?? "",
      event.event_type ?? "",
      event.from ?? "",
      event.to ?? "",
      event.label ?? "",
    ].join("::");
    if (key === lastKey) continue;
    out.push(event);
    lastKey = key;
  }
  return out;
}

export function filterActivityEvents(events: SwarmEvent[], kindFilter: string): SwarmEvent[] {
  if (kindFilter === "all") return events;
  if (!kindFilter || kindFilter === "useful") {
    return events.filter((e) => !isLowSignalEvent(e));
  }
  return events.filter((e) => e.kind === kindFilter);
}

function humanizeEventType(eventType: string | undefined): string {
  return String(eventType || "event")
    .toLowerCase()
    .replace(/_/g, " ");
}

export function activityHeadline(e: SwarmEvent): string {
  const label = String(e.label || "").trim();
  if (label && label !== e.from && label !== e.to) return label;
  const eventType = String(e.event_type || "").toUpperCase();
  if (eventType === "PROJECT_BRIEF_EXPANDED") {
    const preview = String(e.meta?.payload?.preview || e.meta?.preview || "").trim();
    if (preview) return `Brief expanded · ${preview.slice(0, 160)}`;
    return "Brief expanded with product defaults";
  }
  return humanizeEventType(e.event_type);
}

export function activityDetail(e: SwarmEvent): string {
  const parts: string[] = [];
  if (e.from) parts.push(e.from);
  if (e.to && e.to !== e.from) parts.push(`→ ${e.to}`);
  if (e.event_type) parts.push(humanizeEventType(e.event_type));
  const defaults = e.meta?.payload?.category_defaults;
  if (Array.isArray(defaults) && defaults.length) {
    parts.push(`Assumed: ${defaults.slice(0, 3).join(", ")}`);
  }
  return parts.join(" · ");
}

function kindColor(kind: string | undefined, eventType?: string | undefined): string {
  // Server kinds: thought, message, learning, rag, task, stage, ingest, convo, project.
  // Success/failure coloring is derived from the underlying event_type name
  // because the kind alone is too coarse (e.g. both TASK_COMPLETED and
  // TASK_FAILED share kind="task").
  const et = (eventType || "").toUpperCase();
  if (
    et.endsWith("_FAILED") ||
    et.endsWith("_FAILED_FINAL") ||
    et === "ERROR"
  ) {
    return "bg-status-red/20 text-status-red border-status-red/30";
  }
  if (
    et.endsWith("_COMPLETED") ||
    et === "INGEST_COMPLETE" ||
    et === "PIPELINE_COMPLETED"
  ) {
    return "bg-status-green/20 text-status-green border-status-green/30";
  }
  switch (kind) {
    case "task":
    case "stage":
    case "message":
    case "convo":
      return "bg-accent-soft text-accent border-accent-line";
    case "thought":
    case "learning":
    case "rag":
      return "bg-status-yellow/20 text-status-yellow border-status-yellow/30";
    case "ingest":
    case "project":
      return "bg-bg-3 text-text-primary border-border";
    default:
      return "bg-bg-3 text-text-dim border-border";
  }
}
