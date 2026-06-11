import type { FleetSlotStatus } from "../api/client";
import StatusPill from "./StatusPill";

const STATE_COLORS: Record<string, string> = {
  idle: "border-border bg-bg-3/60",
  building: "border-accent/50 bg-accent-soft glow-accent",
  learning: "border-amber-line bg-amber-soft",
  error: "border-status-red/40 bg-status-red/10",
};

export default function FleetGrid({
  slots,
  fleetSize = 20,
}: {
  slots: FleetSlotStatus[];
  fleetSize?: number;
}) {
  const byId = new Map(slots.map((s) => [s.slot_id, s]));
  const cells = Array.from({ length: fleetSize }, (_, i) => byId.get(i) ?? { slot_id: i, state: "idle" });

  return (
    <div className="space-y-3">
      <div
        className="grid gap-1.5"
        style={{ gridTemplateColumns: "repeat(auto-fill, minmax(2.75rem, 1fr))" }}
        title="Fleet slots — cyan pulse = active"
      >
        {cells.map((slot) => {
          const active = slot.state !== "idle";
          const color = STATE_COLORS[slot.state] ?? STATE_COLORS.idle;
          return (
            <div
              key={slot.slot_id}
              className={[
                "relative aspect-square rounded border flex flex-col items-center justify-center gap-0.5 transition-colors",
                color,
                active ? "animate-none" : "",
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
