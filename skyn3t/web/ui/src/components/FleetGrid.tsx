import { useEffect, useRef, useState } from "react";
import type { FleetSlotStatus } from "../api/client";
import StatusPill from "./StatusPill";
import Sparkline from "./Sparkline";
import { useSwarm, type SwarmEvent } from "../context/SwarmProvider";

const STATE_COLORS: Record<string, string> = {
  idle: "border-border bg-bg-3/60",
  building: "border-accent/50 bg-accent-soft glow-accent",
  learning: "border-amber-line bg-amber-soft",
  error: "border-status-red/40 bg-status-red/10",
};

/**
 * Resolve which fleet slot a swarm task/stage event belongs to. Pure & total.
 * Matches the event's slug (meta.payload.slug || project_slug || session id)
 * against a slot's current_slug, then falls back to the event `from` agent name
 * compared against the slot's current brief/slug. Returns the slot_id or null.
 * Exported for the colocated test.
 */
export function matchSlotForEvent(
  ev: { from?: string; meta?: SwarmEvent["meta"] } | null | undefined,
  slots: FleetSlotStatus[],
): number | null {
  if (!ev || !slots.length) return null;
  const slug = (eventSlugSafe(ev) ?? "").toLowerCase();
  if (slug) {
    for (const s of slots) {
      const cur = (s.current_slug ?? "").toLowerCase();
      if (cur && (cur === slug || cur.includes(slug) || slug.includes(cur))) {
        return s.slot_id;
      }
    }
  }
  const from = (ev.from ?? "").toLowerCase();
  if (from) {
    for (const s of slots) {
      const cur = (s.current_slug ?? "").toLowerCase();
      const brief = (s.current_brief ?? "").toLowerCase();
      if ((cur && cur.includes(from)) || (brief && brief.includes(from))) {
        return s.slot_id;
      }
    }
  }
  return null;
}

/** eventSlug from the provider is the source of truth, but keep a tolerant local
 * extractor so this stays pure/testable without a live provider. */
function eventSlugSafe(ev: { meta?: SwarmEvent["meta"] }): string | null {
  const p = ev.meta?.payload as Record<string, unknown> | undefined;
  const slug = (p?.project_slug ?? p?.slug) as string | undefined;
  if (slug) return slug;
  if (ev.meta?.session_id) return ev.meta.session_id;
  return null;
}

/**
 * Subscribe to live task/stage swarm events and flash the matching slot.
 * Layered on top of the 10s poll — never the source of slot truth. Returns the
 * set of slot_ids that should currently render a live flash, plus a rolling
 * busy-count trend for the sparkline.
 */
function useFleetLiveFlash(slots: FleetSlotStatus[]): {
  flashed: Set<number>;
  busyTrend: number[];
} {
  // Depend on the STABLE subscribe fn (useCallback([]) in the provider), NOT
  // the whole swarm context — the context value's identity changes on every
  // incoming event, which would otherwise re-run this effect and its cleanup
  // (clearing all pending flash-clear timers) on each event, making the cyan
  // flash sticky instead of a ~900ms transient.
  const { subscribe } = useSwarm();
  const [flashed, setFlashed] = useState<Set<number>>(() => new Set());
  const [busyTrend, setBusyTrend] = useState<number[]>([]);
  const slotsRef = useRef(slots);
  slotsRef.current = slots;
  const timers = useRef<Map<number, number>>(new Map());

  // Flash a slot for ~900ms on a matching task/stage event.
  useEffect(() => {
    const onEvent = (e: SwarmEvent) => {
      const id = matchSlotForEvent(e, slotsRef.current);
      if (id == null) return;
      setFlashed((prev) => {
        if (prev.has(id)) return prev;
        const next = new Set(prev);
        next.add(id);
        return next;
      });
      const existing = timers.current.get(id);
      if (existing) window.clearTimeout(existing);
      const t = window.setTimeout(() => {
        timers.current.delete(id);
        setFlashed((prev) => {
          if (!prev.has(id)) return prev;
          const next = new Set(prev);
          next.delete(id);
          return next;
        });
      }, 900);
      timers.current.set(id, t);
    };
    const offTask = subscribe("task", onEvent);
    const offStage = subscribe("stage", onEvent);
    return () => {
      offTask();
      offStage();
      timers.current.forEach((t) => window.clearTimeout(t));
      timers.current.clear();
    };
  }, [subscribe]);

  // Sample busy-slot count on each poll snapshot for a trend sparkline.
  const busyNow = slots.filter((s) => s.state !== "idle").length;
  useEffect(() => {
    setBusyTrend((prev) => {
      const next = [...prev, busyNow];
      return next.length > 40 ? next.slice(next.length - 40) : next;
    });
  }, [busyNow]);

  return { flashed, busyTrend };
}

export default function FleetGrid({
  slots,
  fleetSize = 20,
}: {
  slots: FleetSlotStatus[];
  fleetSize?: number;
}) {
  const { flashed, busyTrend } = useFleetLiveFlash(slots);
  const byId = new Map(slots.map((s) => [s.slot_id, s]));
  const cells = Array.from({ length: fleetSize }, (_, i) => byId.get(i) ?? { slot_id: i, state: "idle" });
  const busy = slots.filter((s) => s.state !== "idle").length;

  return (
    <div className="space-y-3">
      <div
        className="grid gap-1.5"
        style={{ gridTemplateColumns: "repeat(auto-fill, minmax(2.75rem, 1fr))" }}
        role="img"
        aria-label={`Fleet grid: ${busy} of ${fleetSize} slots active`}
        title="Fleet slots — cyan pulse = active"
      >
        {cells.map((slot) => {
          const active = slot.state !== "idle";
          const isFlash = flashed.has(slot.slot_id);
          const color = STATE_COLORS[slot.state] ?? STATE_COLORS.idle;
          return (
            <div
              key={slot.slot_id}
              className={[
                "relative aspect-square rounded border flex flex-col items-center justify-center gap-0.5 transition-all duration-300",
                isFlash ? "border-accent bg-accent-soft glow-accent ring-1 ring-accent/60" : color,
              ].join(" ")}
              title={slotTitle(slot)}
            >
              <span className="text-[0.55rem] font-mono text-text-dim">{slot.slot_id}</span>
              {active && (
                <span
                  className={[
                    "w-1 h-1 rounded-full",
                    slot.state === "building" ? "live-dot" : "bg-amber",
                  ].join(" ")}
                />
              )}
            </div>
          );
        })}
      </div>

      {busyTrend.length > 1 && (
        <div className="flex items-center gap-2 text-[0.6rem] text-text-dim">
          <span className="uppercase tracking-[0.14em]">busy trend</span>
          <span className="text-accent flex-1 min-w-0">
            <Sparkline
              values={busyTrend}
              width={160}
              height={18}
              strokeWidth={1.25}
              min={0}
              max={Math.max(1, fleetSize)}
              className="w-full"
              aria-label={`Busy-slot trend, currently ${busy} of ${fleetSize}`}
            />
          </span>
          <span className="font-mono text-text-secondary tabular-nums">
            {busy}/{fleetSize}
          </span>
        </div>
      )}

      <div className="flex flex-wrap gap-3 text-[0.65rem] text-text-dim">
        <Legend color="border-border bg-bg-3" label="idle" />
        <Legend color="border-accent/50 bg-accent-soft" label="building" pulse />
        <Legend color="border-amber-line bg-amber-soft" label="learning" />
        <Legend color="border-status-red/40 bg-status-red/10" label="error" />
      </div>
    </div>
  );
}

function Legend({
  color,
  label,
  pulse,
}: {
  color: string;
  label: string;
  pulse?: boolean;
}) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className={["w-3 h-3 rounded border", color].join(" ")}>
        {pulse && <span className="sr-only">pulse</span>}
      </span>
      {label}
    </span>
  );
}

function slotTitle(slot: FleetSlotStatus): string {
  const parts = [`Slot ${slot.slot_id}: ${slot.state}`];
  if (slot.current_slug) parts.push(slot.current_slug);
  if (slot.learning_kind) parts.push(slot.learning_kind);
  if (slot.last_error) parts.push(slot.last_error);
  return parts.join(" · ");
}

export function FleetSlotList({ slots }: { slots: FleetSlotStatus[] }) {
  const active = slots.filter((s) => s.state !== "idle");
  if (active.length === 0) {
    return <p className="text-text-dim text-sm">All slots idle.</p>;
  }
  return (
    <ul className="space-y-2 max-h-48 overflow-y-auto">
      {active.map((s) => (
        <li
          key={s.slot_id}
          className="flex items-center gap-2 text-xs border border-border rounded px-2 py-1.5 bg-bg-3/50"
        >
          <span className="font-mono text-text-dim w-5 shrink-0">{s.slot_id}</span>
          <StatusPill status={s.state} pulse />
          <span className="font-mono text-text-secondary truncate min-w-0">
            {s.current_slug ?? s.learning_kind ?? "—"}
          </span>
        </li>
      ))}
    </ul>
  );
}
