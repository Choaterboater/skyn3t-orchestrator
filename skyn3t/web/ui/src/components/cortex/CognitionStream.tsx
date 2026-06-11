import { useEffect, useRef, useState } from "react";

import type { SwarmEvent } from "../../context/SwarmProvider";
import { useSwarm, projectSubKind, eventSlug } from "../../context/SwarmProvider";

// ============================================================
// CognitionStream — the right-rail "inner monologue" of the swarm.
//
// Subscribes to thought / learning / convo (LLM exchange) events and
// renders them newest-first as a bounded, scannable feed. LLM exchanges
// become collapsible cards exposing model / backend / duration chips and
// truncated prompt+response. Project events are tagged by their real
// sub-kind via projectSubKind().
// ============================================================

const STREAM_KINDS = ["thought", "learning", "convo", "project"] as const;
const MAX_ITEMS = 80;

interface StreamItem {
  id: number;
  e: SwarmEvent;
}

export interface CognitionStreamProps {
  className?: string;
}

export default function CognitionStream({ className = "" }: CognitionStreamProps) {
  const { subscribe } = useSwarm();
  const [items, setItems] = useState<StreamItem[]>([]);
  const [paused, setPaused] = useState(false);
  const pausedRef = useRef(false);
  const idRef = useRef(0);
  const bufferRef = useRef<StreamItem[]>([]);
  const [pending, setPending] = useState(0);

  useEffect(() => {
    pausedRef.current = paused;
    if (!paused && bufferRef.current.length) {
      // Flush what arrived while paused.
      const flush = bufferRef.current;
      bufferRef.current = [];
      setPending(0);
      setItems((prev) => [...flush.reverse(), ...prev].slice(0, MAX_ITEMS));
    }
  }, [paused]);

  useEffect(() => {
    const unsubs = STREAM_KINDS.map((kind) =>
      subscribe(kind, (e) => {
        const item = { id: (idRef.current += 1), e };
        if (pausedRef.current) {
          bufferRef.current = [...bufferRef.current, item].slice(-MAX_ITEMS);
          setPending(bufferRef.current.length);
          return;
        }
        setItems((prev) => [item, ...prev].slice(0, MAX_ITEMS));
      }),
    );
    return () => unsubs.forEach((u) => u());
  }, [subscribe]);

  return (
    <div className={["flex flex-col min-h-0", className].join(" ")}>
      <div className="flex items-center justify-between gap-2 px-3 py-2 border-b border-border bg-bg-3/60 shrink-0">
        <div className="flex items-center gap-2">
          <span className="live-dot" aria-hidden />
          <span className="section-label">Cognition Stream</span>
        </div>
        <button
          type="button"
          onClick={() => setPaused((p) => !p)}
          aria-pressed={paused}
          className={[
            "text-[0.62rem] uppercase tracking-wider px-2 py-0.5 rounded border transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent",
            paused
              ? "border-amber-line bg-amber-soft text-amber"
              : "border-border text-text-dim hover:text-text-primary hover:border-border-strong",
          ].join(" ")}
        >
          {paused ? (
            <>
              <i className="fa-solid fa-play mr-1" aria-hidden />
              resume{pending > 0 ? ` (${pending})` : ""}
            </>
          ) : (
            <>
              <i className="fa-solid fa-pause mr-1" aria-hidden />
              pause
            </>
          )}
        </button>
      </div>

      <div
        className="flex-1 min-h-0 overflow-y-auto"
        aria-live="polite"
        aria-label="Live cognition events"
      >
        {items.length === 0 ? (
          <div className="p-6 text-center space-y-1">
            <div className="text-text-secondary text-sm">No thoughts yet.</div>
            <div className="text-text-dim text-xs font-mono">
              Thoughts, learnings and LLM exchanges land here as they fire.
            </div>
          </div>
        ) : (
          <ul className="divide-y divide-border/60">
            {items.map((it) => (
              <CognitionRow key={it.id} e={it.e} />
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function CognitionRow({ e }: { e: SwarmEvent }) {
  if (e.kind === "convo") return <ConvoCard e={e} />;
  return <ThoughtRow e={e} />;
}

function ThoughtRow({ e }: { e: SwarmEvent }) {
  const tone = kindTone(e);
  const label = (e.label ?? "").trim() || humanize(displaySubKind(e));
  const slug = eventSlug(e);
  return (
    <li className="px-3 py-2 group">
      <div className="flex items-center gap-2 mb-0.5">
        <span
          className={[
            "text-[0.58rem] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded border shrink-0",
            tone,
          ].join(" ")}
        >
          {displaySubKind(e)}
        </span>
        <span className="text-[0.6rem] text-text-dim font-mono ml-auto shrink-0">
          {formatTs(e.ts)}
        </span>
      </div>
      <div className="text-sm text-text-primary break-words leading-snug">
        {label}
      </div>
      {(e.from || slug) && (
        <div className="text-[0.62rem] font-mono text-text-dim mt-0.5 truncate">
          {e.from && <span>{e.from}</span>}
          {e.from && slug && <span> · </span>}
          {slug && <span className="text-text-secondary">{slug}</span>}
        </div>
      )}
    </li>
  );
}

function ConvoCard({ e }: { e: SwarmEvent }) {
  const [open, setOpen] = useState(false);
  const model = e.meta?.model;
  const backend = e.meta?.backend;
  const duration = e.meta?.duration_ms;
  const prompt = (e.meta?.prompt ?? "").trim();
  const response = (e.meta?.response ?? "").trim();
  const title = (e.label ?? "").trim() || model || backend || "LLM exchange";

  return (
    <li className="px-3 py-2 bg-accent-soft/30">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="w-full text-left group focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent rounded"
      >
        <div className="flex items-center gap-2 mb-1">
          <i
            className={[
              "fa-solid text-[0.6rem] text-accent transition-transform",
              open ? "fa-chevron-down" : "fa-chevron-right",
            ].join(" ")}
            aria-hidden
          />
          <span className="text-[0.58rem] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded border border-accent-line bg-accent-soft text-accent shrink-0">
            convo
          </span>
          <span className="text-sm text-text-primary truncate">{title}</span>
          <span className="text-[0.6rem] text-text-dim font-mono ml-auto shrink-0">
            {formatTs(e.ts)}
          </span>
        </div>
        <div className="flex flex-wrap gap-1.5 pl-4">
          {model && <Chip icon="fa-microchip" text={String(model)} />}
          {backend && <Chip icon="fa-server" text={String(backend)} tone="amber" />}
          {typeof duration === "number" && (
            <Chip icon="fa-stopwatch" text={formatDuration(duration)} tone="dim" />
          )}
        </div>
      </button>

      {open && (
        <div className="mt-2 pl-4 space-y-2">
          {prompt && (
            <Exchange role="prompt" text={prompt} />
          )}
          {response && (
            <Exchange role="response" text={response} />
          )}
          {!prompt && !response && (
            <div className="text-[0.7rem] text-text-dim font-mono">
              No prompt/response captured for this exchange.
            </div>
          )}
        </div>
      )}
    </li>
  );
}

function Exchange({ role, text }: { role: "prompt" | "response"; text: string }) {
  const isPrompt = role === "prompt";
  return (
    <div>
      <div
        className={[
          "text-[0.55rem] font-mono uppercase tracking-wider mb-0.5",
          isPrompt ? "text-text-dim" : "text-accent",
        ].join(" ")}
      >
        {role}
      </div>
      <pre
        className={[
          "text-[0.72rem] font-mono whitespace-pre-wrap break-words rounded border px-2 py-1.5 max-h-40 overflow-y-auto",
          isPrompt
            ? "border-border bg-bg-3/60 text-text-secondary"
            : "border-accent-line bg-accent-soft/40 text-text-primary",
        ].join(" ")}
      >
        {truncate(text, 900)}
      </pre>
    </div>
  );
}

function Chip({
  icon,
  text,
  tone = "accent",
}: {
  icon: string;
  text: string;
  tone?: "accent" | "amber" | "dim";
}) {
  const cls =
    tone === "amber"
      ? "border-amber-line bg-amber-soft text-amber"
      : tone === "dim"
        ? "border-border bg-bg-3 text-text-dim"
        : "border-accent-line bg-accent-soft text-accent";
  return (
    <span
      className={[
        "inline-flex items-center gap-1 text-[0.58rem] font-mono px-1.5 py-0.5 rounded border",
        cls,
      ].join(" ")}
    >
      <i className={["fa-solid", icon].join(" ")} aria-hidden />
      <span className="truncate max-w-[10rem]">{text}</span>
    </span>
  );
}

// ---------- helpers ----------

/** Display label for an event's sub-kind (project events use payload.kind). */
export function displaySubKind(e: SwarmEvent): string {
  if (e.kind === "project") {
    const sub = projectSubKind(e);
    return humanize(sub).toLowerCase();
  }
  return e.kind || "event";
}

function kindTone(e: SwarmEvent): string {
  const et = (e.event_type || "").toUpperCase();
  const sub = e.kind === "project" ? projectSubKind(e).toUpperCase() : et;
  if (sub.endsWith("_FAILED") || sub === "ERROR") {
    return "border-status-red/30 bg-status-red/10 text-status-red";
  }
  if (sub.endsWith("_COMPLETED")) {
    return "border-status-green/30 bg-status-green/10 text-status-green";
  }
  switch (e.kind) {
    case "thought":
      return "border-accent-line bg-accent-soft text-accent";
    case "learning":
      return "border-amber-line bg-amber-soft text-amber";
    case "project":
      return "border-border bg-bg-3 text-text-secondary";
    default:
      return "border-border bg-bg-3 text-text-dim";
  }
}

export function humanize(s: string | undefined): string {
  return String(s || "event")
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase())
    .trim();
}

export function formatDuration(ms: number): string {
  if (!isFinite(ms) || ms < 0) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(ms < 10000 ? 2 : 1)}s`;
}

function formatTs(ts: unknown): string {
  const d = parseTs(ts);
  if (!d) return "—";
  return d.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function parseTs(ts: unknown): Date | null {
  if (ts == null) return null;
  if (typeof ts === "number") {
    const d = new Date(ts * 1000);
    return isNaN(d.getTime()) ? null : d;
  }
  if (typeof ts === "string") {
    const d = new Date(ts);
    return isNaN(d.getTime()) ? null : d;
  }
  return null;
}

function truncate(s: string, n: number): string {
  if (s.length <= n) return s;
  return `${s.slice(0, n - 1)}…`;
}
