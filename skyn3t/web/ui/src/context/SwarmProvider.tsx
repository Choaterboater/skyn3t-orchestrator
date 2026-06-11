// SwarmProvider — single app-wide reconnecting WebSocket to /ws/swarm.
//
// Phase 4 command-center foundation. Everything live in the dashboard
// (cortex graph, studio stage stream, overview pulse, sidebar status)
// hangs off ONE socket so we never open N sockets for N panes and never
// miss a burst. The reconnect machinery is lifted verbatim from
// ActivityPage's StrictMode-safe pattern: a `cancelled` flag checked in
// every callback, explicit handler-detach on cleanup, 2s->30s backoff,
// and ?token= from getAuthToken().
//
// Consumers get three layers of access:
//   - useSwarm()                — full context (events ring, status, counts…)
//   - useSwarmEvents(filter,n)  — memoized filtered slice for render
//   - subscribe(kind, cb)       — SYNCHRONOUS firehose for toasts / EKG /
//                                 anything that must react to a single event
//                                 BEFORE React batches the next state flush.

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { ReactNode } from "react";

import { api, getAuthToken } from "../api/client";

// ---------------------------------------------------------------------------
// Types — exported as the binding contract for cortex-brain / studio-stream /
// overview-pulse / sidebar-status / app-integration.
// ---------------------------------------------------------------------------

// VERIFIED against app.py _project_swarm_event (lines 281-289). NOTE: ts is an
// ISO string from the live server, but snapshot backfill may carry legacy
// numeric epoch-seconds; treat ts as string|number and parse defensively.
export type SwarmEvent = {
  kind: string;
  ts: string;
  from?: string;
  to?: string;
  label: string;
  event_type: string;
  meta?: {
    task_id?: string | null;
    session_id?: string | null;
    correlation_id?: string | null;
    payload?: Record<string, any>;
    prompt?: string;
    response?: string;
    model?: string;
    backend?: string;
    duration_ms?: number;
  };
};

export type SwarmConnState = "connecting" | "open" | "closed";

export interface SwarmContextValue {
  /** newest-first, bounded ring buffer (max bufferSize) */
  events: SwarmEvent[];
  /** live connection state */
  status: SwarmConnState;
  /** epoch ms of most recent event (for EKG/pulse/idle detection) */
  lastEventAt: number | null;
  /** returns unsubscribe; '*' = all kinds. Fires synchronously inside onmessage. */
  subscribe(kind: string | "*", cb: (e: SwarmEvent) => void): () => void;
  /** running per-kind counter since mount (cheap pulse fuel) */
  counts: Record<string, number>;
}

// ---------------------------------------------------------------------------
// PURE helpers — exported and unit-tested in swarmEvents.test.ts.
// They never touch the DOM, React, or the network.
// ---------------------------------------------------------------------------

/**
 * Tolerant parse of a raw WS / snapshot payload into a stable SwarmEvent.
 * Coerces missing fields to safe defaults, normalizes ts to an ISO string
 * (legacy numeric epoch-seconds -> ISO), and guarantees `meta` exists only
 * when the source carried it (so consumers can rely on the field shape).
 */
export function normalizeSwarmEvent(raw: any): SwarmEvent {
  const src = raw && typeof raw === "object" ? raw : {};

  const kind = typeof src.kind === "string" && src.kind ? src.kind : "event";
  const event_type =
    typeof src.event_type === "string" && src.event_type
      ? src.event_type
      : "UNKNOWN";
  const label =
    typeof src.label === "string"
      ? src.label
      : src.label != null
        ? String(src.label)
        : "";

  const out: SwarmEvent = {
    kind,
    ts: normalizeTs(src.ts),
    label,
    event_type,
  };

  if (typeof src.from === "string") out.from = src.from;
  if (typeof src.to === "string") out.to = src.to;

  if (src.meta && typeof src.meta === "object") {
    const m = src.meta as Record<string, any>;
    const meta: NonNullable<SwarmEvent["meta"]> = {};
    if (m.task_id !== undefined) meta.task_id = m.task_id;
    if (m.session_id !== undefined) meta.session_id = m.session_id;
    if (m.correlation_id !== undefined) meta.correlation_id = m.correlation_id;
    if (m.payload && typeof m.payload === "object") meta.payload = m.payload;
    if (typeof m.prompt === "string") meta.prompt = m.prompt;
    if (typeof m.response === "string") meta.response = m.response;
    if (typeof m.model === "string") meta.model = m.model;
    if (typeof m.backend === "string") meta.backend = m.backend;
    if (typeof m.duration_ms === "number") meta.duration_ms = m.duration_ms;
    out.meta = meta;
  }

  return out;
}

/**
 * Normalize a ts value (ISO string OR legacy numeric epoch-seconds) into an
 * ISO 8601 string. Unparseable input falls back to "now" so downstream
 * Date parsing never produces Invalid Date.
 */
function normalizeTs(ts: unknown): string {
  if (typeof ts === "string") {
    const t = ts.trim();
    if (!t) return new Date().toISOString();
    // Numeric-looking string: treat as epoch (seconds if it looks small).
    if (/^-?\d+(\.\d+)?$/.test(t)) {
      return epochToIso(Number(t));
    }
    const d = new Date(t);
    return isNaN(d.getTime()) ? new Date().toISOString() : d.toISOString();
  }
  if (typeof ts === "number" && isFinite(ts)) {
    return epochToIso(ts);
  }
  return new Date().toISOString();
}

function epochToIso(n: number): string {
  // Heuristic: epoch-seconds (~1e9..1e10) -> *1000; ms stays as-is.
  const ms = Math.abs(n) < 1e12 ? n * 1000 : n;
  const d = new Date(ms);
  return isNaN(d.getTime()) ? new Date().toISOString() : d.toISOString();
}

/**
 * Parse an event ts to epoch-ms for EKG / pulse / lastEventAt. PURE.
 * Returns null when unparseable.
 */
export function eventTsMs(e: Pick<SwarmEvent, "ts">): number | null {
  const ts: unknown = e.ts;
  if (typeof ts === "number" && isFinite(ts)) {
    return Math.abs(ts) < 1e12 ? ts * 1000 : ts;
  }
  if (typeof ts === "string") {
    const t = ts.trim();
    if (!t) return null;
    if (/^-?\d+(\.\d+)?$/.test(t)) {
      const n = Number(t);
      return Math.abs(n) < 1e12 ? n * 1000 : n;
    }
    const d = new Date(t);
    return isNaN(d.getTime()) ? null : d.getTime();
  }
  return null;
}

/**
 * Resolve the meaningful sub-kind for an event.
 *
 * PROJECT_* events ride EventType.SYSTEM_ALERT, so the top-level
 * event_type is 'SYSTEM_ALERT' and kind is 'project'; the REAL project
 * sub-type (e.g. PROJECT_STAGE_COMPLETED) lives in meta.payload.kind.
 * For everything else the top-level event_type IS the sub-kind. PURE.
 */
export function projectSubKind(e: SwarmEvent): string {
  if (e.kind === "project") {
    const payloadKind = e.meta?.payload?.kind;
    if (typeof payloadKind === "string" && payloadKind) return payloadKind;
  }
  return e.event_type;
}

/**
 * Best-effort stable correlation slug for an event:
 *   meta.payload.project_slug || meta.payload.slug || meta.session_id || null
 * Used to group cortex graph nodes / studio stages by project/session. PURE.
 */
export function eventSlug(e: SwarmEvent): string | null {
  const payload = e.meta?.payload;
  if (payload) {
    if (typeof payload.project_slug === "string" && payload.project_slug) {
      return payload.project_slug;
    }
    if (typeof payload.slug === "string" && payload.slug) {
      return payload.slug;
    }
  }
  const session = e.meta?.session_id;
  if (typeof session === "string" && session) return session;
  return null;
}

// ---------------------------------------------------------------------------
// Context
// ---------------------------------------------------------------------------

const SwarmContext = createContext<SwarmContextValue | null>(null);

const DEFAULT_BUFFER = 500;

export function SwarmProvider(props: {
  children: ReactNode;
  bufferSize?: number;
}) {
  const bufferSize = props.bufferSize ?? DEFAULT_BUFFER;

  const [events, setEvents] = useState<SwarmEvent[]>([]);
  const [status, setStatus] = useState<SwarmConnState>("connecting");
  const [lastEventAt, setLastEventAt] = useState<number | null>(null);
  const [counts, setCounts] = useState<Record<string, number>>({});

  // Synchronous subscriber registry. Keyed by kind ('*' = all). Fired inside
  // onmessage BEFORE setState batching so consumers never miss a burst.
  const subsRef = useRef<Map<string, Set<(e: SwarmEvent) => void>>>(new Map());
  const wsRef = useRef<WebSocket | null>(null);
  const bufferSizeRef = useRef(bufferSize);
  bufferSizeRef.current = bufferSize;

  const subscribe = useCallback(
    (kind: string | "*", cb: (e: SwarmEvent) => void) => {
      const map = subsRef.current;
      let set = map.get(kind);
      if (!set) {
        set = new Set();
        map.set(kind, set);
      }
      set.add(cb);
      return () => {
        const s = subsRef.current.get(kind);
        if (s) {
          s.delete(cb);
          if (s.size === 0) subsRef.current.delete(kind);
        }
      };
    },
    [],
  );

  // Fire synchronous subscribers for a single event. Errors in one callback
  // never break the dispatch loop (a noisy toast must not kill the EKG).
  const dispatch = useCallback((e: SwarmEvent) => {
    const map = subsRef.current;
    const exact = map.get(e.kind);
    if (exact) {
      for (const cb of exact) {
        try {
          cb(e);
        } catch {
          /* subscriber error swallowed */
        }
      }
    }
    const all = map.get("*");
    if (all) {
      for (const cb of all) {
        try {
          cb(e);
        } catch {
          /* subscriber error swallowed */
        }
      }
    }
  }, []);

  // Commit a batch of already-normalized events (newest-first within the
  // batch) into the ring buffer + counts + lastEventAt in a single setState
  // sweep per message slice.
  const commit = useCallback((batch: SwarmEvent[]) => {
    if (batch.length === 0) return;
    setEvents((prev) => [...batch, ...prev].slice(0, bufferSizeRef.current));
    setCounts((prev) => {
      const next = { ...prev };
      for (const e of batch) next[e.kind] = (next[e.kind] ?? 0) + 1;
      return next;
    });
    let newest: number | null = null;
    for (const e of batch) {
      const ms = eventTsMs(e);
      if (ms != null && (newest == null || ms > newest)) newest = ms;
    }
    // Prefer the event's own ts; fall back to wall-clock arrival so the
    // pulse keeps beating even if a server omits ts.
    setLastEventAt(newest ?? Date.now());
  }, []);

  // Seed the ring buffer once on mount from the REST snapshot so the buffer
  // is non-empty even when the live socket is offline. recent_messages is
  // oldest-first on the wire; reverse to newest-first before committing.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const snap = await api.swarmSnapshot();
        if (cancelled) return;
        const msgs = snap?.recent_messages;
        if (!Array.isArray(msgs) || msgs.length === 0) return;
        const seeded = [...msgs]
          .reverse()
          .map((m) => normalizeSwarmEvent(m));
        // Seed without firing subscribers (these are historical, not live) and
        // without nuking a live buffer that may have already filled in.
        setEvents((prev) =>
          prev.length ? prev : seeded.slice(0, bufferSizeRef.current),
        );
        setCounts((prev) => {
          if (Object.keys(prev).length) return prev;
          const next: Record<string, number> = {};
          for (const e of seeded) next[e.kind] = (next[e.kind] ?? 0) + 1;
          return next;
        });
      } catch {
        /* snapshot unavailable — live socket will fill the buffer */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Single reconnecting WebSocket. Reuses ActivityPage's StrictMode-safe
  // pattern verbatim: cancelled flag in every callback, explicit handler
  // detach on cleanup, 2s->30s backoff, ?token= from getAuthToken().
  useEffect(() => {
    let cancelled = false;
    let retry = 0;
    let timer: number | null = null;

    const connect = () => {
      if (cancelled) return;
      setStatus((s) => (s === "open" ? s : "connecting"));
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
        setStatus("open");
      };
      ws.onclose = () => {
        if (cancelled) return;
        setStatus("closed");
        scheduleReconnect();
      };
      ws.onerror = () => {
        if (cancelled) return;
        setStatus("closed");
      };
      ws.onmessage = (e) => {
        if (cancelled) return;
        try {
          const msg = JSON.parse(e.data);
          if (msg?.type === "swarm" && msg.data) {
            const ev = normalizeSwarmEvent(msg.data);
            // Synchronous firehose FIRST so toasts/EKG never miss it…
            dispatch(ev);
            // …then the throttled state commit for render consumers.
            commit([ev]);
          }
        } catch {
          /* ignore malformed frame */
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
        // Detach handlers before close so the dying socket doesn't fire
        // onclose -> scheduleReconnect after we've unmounted.
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
  }, [commit, dispatch]);

  const value = useMemo<SwarmContextValue>(
    () => ({ events, status, lastEventAt, subscribe, counts }),
    [events, status, lastEventAt, subscribe, counts],
  );

  return (
    <SwarmContext.Provider value={value}>
      {props.children}
    </SwarmContext.Provider>
  );
}

export function useSwarm(): SwarmContextValue {
  const ctx = useContext(SwarmContext);
  if (!ctx) {
    throw new Error("useSwarm must be used within a <SwarmProvider>");
  }
  return ctx;
}

/**
 * Memoized selector convenience: returns a (optionally filtered, optionally
 * capped) newest-first slice of the live event buffer. Re-renders only when
 * the underlying buffer changes.
 */
export function useSwarmEvents(
  filter?: (e: SwarmEvent) => boolean,
  limit?: number,
): SwarmEvent[] {
  const { events } = useSwarm();
  return useMemo(() => {
    let out = filter ? events.filter(filter) : events;
    if (typeof limit === "number" && limit >= 0 && out.length > limit) {
      out = out.slice(0, limit);
    }
    return out;
  }, [events, filter, limit]);
}
