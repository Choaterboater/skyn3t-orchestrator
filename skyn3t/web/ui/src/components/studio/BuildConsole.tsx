import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import {
  eventSlug,
  projectSubKind,
  useSwarm,
  type SwarmEvent,
} from "../../context/SwarmProvider";
import Sparkline, { MiniBars } from "../Sparkline";

// ============================================================================
// BuildConsole — live build telemetry for a single Studio project.
//
// StudioPage keeps its 4s REST poll for artifacts/verdict; this component is
// the *live* layer: it taps the app-wide swarm WebSocket (via useSwarm) and
// shows stage transitions, agent thoughts, and LLM exchanges as they happen,
// scoped to this build by slug / session id.
//
// Design intent ("Command Center Atelier"): a terminal-grade console with a
// living scanline header, latency telemetry rendered as real charts driven by
// stage durations, cyan for live data and amber for cognition. Newest line at
// the bottom (like a real log) with auto-scroll that pauses when you read.
// ============================================================================

export interface BuildConsoleProps {
  slug: string;
  sessionId?: string | null;
  /** Max lines retained in the local console buffer. Default 400. */
  maxLines?: number;
  className?: string;
}

// ----------------------------------------------------------------------------
// PURE helpers (also exercised by buildConsole.test.ts) — no React, no DOM.
// ----------------------------------------------------------------------------

/** Tolerant ts parse: ISO string (server) or legacy numeric epoch-seconds. */
export function parseConsoleTs(ts: unknown): Date | null {
  if (ts == null) return null;
  if (typeof ts === "number") {
    const d = new Date(ts * 1000);
    return Number.isNaN(d.getTime()) ? null : d;
  }
  if (typeof ts === "string") {
    const trimmed = ts.trim();
    // A bare integer string (no date separators) → legacy epoch-seconds.
    if (trimmed !== "" && /^\d+$/.test(trimmed)) {
      const d = new Date(Number(trimmed) * 1000);
      return Number.isNaN(d.getTime()) ? null : d;
    }
    const d = new Date(ts);
    return Number.isNaN(d.getTime()) ? null : d;
  }
  return null;
}

/**
 * Keep only the events that belong to this build.
 *
 * The SYSTEM_ALERT gotcha: PROJECT_* events ride event_type==='SYSTEM_ALERT'
 * with kind==='project', so the slug is NOT at the top level — it lives in
 * meta.payload.project_slug || meta.payload.slug. eventSlug() already encodes
 * that precedence (and falls back to meta.session_id), so we match on it.
 * Non-project lines (thought/convo) carry only meta.session_id, hence the
 * explicit session match too.
 */
export function filterBuildEvents(
  events: SwarmEvent[],
  slug: string,
  sessionId?: string | null,
): SwarmEvent[] {
  if (!Array.isArray(events) || events.length === 0) return [];
  const wantSession = sessionId ? String(sessionId) : null;
  return events.filter((e) => {
    if (!e) return false;
    if (slug && eventSlug(e) === slug) return true;
    const sid = e.meta?.session_id;
    if (wantSession && sid != null && String(sid) === wantSession) return true;
    return false;
  });
}

export type StageStatus = "running" | "completed" | "failed";

export interface StageTimelineEntry {
  name: string;
  agent?: string;
  status: StageStatus;
  startedAt: number | null;
  endedAt: number | null;
  durationMs: number | null;
}

export interface StageTimelineSummary {
  stages: StageTimelineEntry[];
  /** Measured durations (completed or failed stages), oldest→newest. */
  durations: number[];
  total: number;
  completed: number;
  failed: number;
}

// Identify the stage name for a stage/project event. Stage lives at
// meta.payload.stage, falling back to the human label.
function stageNameOf(e: SwarmEvent): string | null {
  const payloadStage = e.meta?.payload?.stage;
  if (typeof payloadStage === "string" && payloadStage.trim()) {
    return payloadStage.trim();
  }
  const name = e.meta?.payload?.name;
  if (typeof name === "string" && name.trim()) return name.trim();
  return null;
}

// Classify a stage event's intent from its (sub-)kind name.
function stagePhase(subKind: string): "start" | "complete" | "fail" | null {
  const k = subKind.toUpperCase();
  if (k.includes("FAIL") || k.includes("ERROR")) return "fail";
  if (k.includes("COMPLETE") || k.endsWith("_DONE") || k.includes("FINISHED")) {
    return "complete";
  }
  if (k.includes("START")) return "start";
  return null;
}

function isStageEvent(e: SwarmEvent): boolean {
  if (e.kind === "stage") return true;
  if (e.kind === "project") {
    const sub = projectSubKind(e).toUpperCase();
    return sub.includes("STAGE") || sub.includes("PIPELINE");
  }
  return false;
}

/**
 * Fold the event stream into an ordered stage timeline with per-stage
 * durations. Pairs STARTED→COMPLETED/FAILED by stage name; a started stage
 * with no terminal event is reported as "running" with a null duration.
 */
export function summarizeStageTimeline(events: SwarmEvent[]): StageTimelineSummary {
  const empty: StageTimelineSummary = {
    stages: [],
    durations: [],
    total: 0,
    completed: 0,
    failed: 0,
  };
  if (!Array.isArray(events) || events.length === 0) return empty;

  const order: string[] = [];
  const byName = new Map<string, StageTimelineEntry>();

  for (const e of events) {
    if (!e || !isStageEvent(e)) continue;
    const name = stageNameOf(e) ?? (e.label || "").trim();
    if (!name) continue;
    const phase = stagePhase(projectSubKind(e));
    if (!phase) continue;

    let entry = byName.get(name);
    if (!entry) {
      entry = {
        name,
        agent: typeof e.from === "string" && e.from ? e.from : undefined,
        status: "running",
        startedAt: null,
        endedAt: null,
        durationMs: null,
      };
      byName.set(name, entry);
      order.push(name);
    }
    const tsMs = parseConsoleTs(e.ts)?.getTime() ?? null;

    if (phase === "start") {
      if (tsMs != null) entry.startedAt = tsMs;
      // Don't downgrade an already-terminal stage back to running.
      if (entry.status === "running") entry.status = "running";
    } else {
      entry.status = phase === "fail" ? "failed" : "completed";
      if (tsMs != null) entry.endedAt = tsMs;
    }

    if (entry.startedAt != null && entry.endedAt != null) {
      entry.durationMs = Math.max(0, entry.endedAt - entry.startedAt);
    }
  }

  const stages = order.map((n) => byName.get(n)!);
  const durations = stages
    .filter((s) => s.durationMs != null)
    .map((s) => s.durationMs as number);

  return {
    stages,
    durations,
    total: stages.length,
    completed: stages.filter((s) => s.status === "completed").length,
    failed: stages.filter((s) => s.status === "failed").length,
  };
}

function humanize(text: string): string {
  return String(text || "")
    .toLowerCase()
    .replace(/_/g, " ")
    .trim();
}

/** Human title for a console line. Prefers a real label, else humanized kind. */
export function buildEventTitle(e: SwarmEvent): string {
  const label = String(e.label || "").trim();
  if (label && label !== e.from && label !== e.to) return label;
  if (e.kind === "project") return humanize(projectSubKind(e));
  if (e.kind === "convo") {
    const model = e.meta?.model;
    return model ? `LLM exchange · ${model}` : "LLM exchange";
  }
  return humanize(e.event_type || e.kind);
}

// ----------------------------------------------------------------------------
// Presentational helpers
// ----------------------------------------------------------------------------

type LineTone = "stage" | "stage-done" | "stage-fail" | "thought" | "learning" | "convo" | "other";

function lineTone(e: SwarmEvent): LineTone {
  if (isStageEvent(e)) {
    const phase = stagePhase(projectSubKind(e));
    if (phase === "fail") return "stage-fail";
    if (phase === "complete") return "stage-done";
    return "stage";
  }
  switch (e.kind) {
    case "thought":
      return "thought";
    case "learning":
      return "learning";
    case "convo":
      return "convo";
    default:
      return "other";
  }
}

const TONE_DOT: Record<LineTone, string> = {
  stage: "bg-accent shadow-[0_0_6px_var(--accent-glow)]",
  "stage-done": "bg-status-green shadow-[0_0_6px_rgba(78,207,154,0.5)]",
  "stage-fail": "bg-status-red shadow-[0_0_6px_rgba(240,113,113,0.5)]",
  thought: "bg-amber shadow-[0_0_6px_var(--amber-glow)]",
  learning: "bg-amber-strong shadow-[0_0_6px_var(--amber-glow)]",
  convo: "bg-chrome-dim",
  other: "bg-text-dim",
};

const TONE_TEXT: Record<LineTone, string> = {
  stage: "text-accent",
  "stage-done": "text-status-green",
  "stage-fail": "text-status-red",
  thought: "text-amber",
  learning: "text-amber-strong",
  convo: "text-chrome",
  other: "text-text-dim",
};

function fmtDuration(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms)) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)}s`;
  const m = Math.floor(ms / 60_000);
  const s = Math.round((ms % 60_000) / 1000);
  return `${m}m${s.toString().padStart(2, "0")}s`;
}

// ----------------------------------------------------------------------------
// Component
// ----------------------------------------------------------------------------

export function BuildConsole({
  slug,
  sessionId,
  maxLines = 400,
  className,
}: BuildConsoleProps) {
  const { events: bufferEvents, status, subscribe } = useSwarm();

  // Local console buffer in CHRONOLOGICAL (oldest→newest) order so we can
  // append at the bottom like a real log. We seed from the provider's
  // newest-first ring buffer, then keep up live via subscribe() — which fires
  // synchronously inside onmessage so we never miss a burst.
  const [lines, setLines] = useState<SwarmEvent[]>([]);
  const seenRef = useRef<Set<string>>(new Set());

  // Reset the buffer the instant the build identity changes — done during
  // render (not in an effect) so it can't race the seed/live effects below,
  // which would otherwise wipe a freshly-seeded buffer on mount.
  const identityRef = useRef<string>("");
  const identity = `${slug}::${sessionId ?? ""}`;
  if (identityRef.current !== identity) {
    identityRef.current = identity;
    seenRef.current = new Set();
    if (lines.length > 0) setLines([]);
  }

  // Stable key for dedupe across seed + live. Falls back to a structural key
  // when no correlation/ts is present.
  const keyOf = (e: SwarmEvent): string => {
    const cid = e.meta?.correlation_id;
    if (cid) return `cid:${cid}`;
    return [
      e.ts ?? "",
      e.kind ?? "",
      e.event_type ?? "",
      e.from ?? "",
      e.label ?? "",
      projectSubKind(e),
    ].join("::");
  };

  // Seed from the shared ring buffer whenever it changes meaningfully. We
  // reverse to chronological and append only genuinely-new lines.
  useEffect(() => {
    const mine = filterBuildEvents(bufferEvents, slug, sessionId);
    if (mine.length === 0) return;
    const chrono = [...mine].reverse();
    setLines((prev) => {
      const next = prev.slice();
      let changed = false;
      for (const e of chrono) {
        const k = keyOf(e);
        if (seenRef.current.has(k)) continue;
        seenRef.current.add(k);
        next.push(e);
        changed = true;
      }
      if (!changed) return prev;
      return next.length > maxLines ? next.slice(next.length - maxLines) : next;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bufferEvents, slug, sessionId, maxLines]);

  // Live tap: synchronous, burst-proof. We still dedupe against the seed.
  useEffect(() => {
    const unsub = subscribe("*", (e) => {
      const mine = filterBuildEvents([e], slug, sessionId);
      if (mine.length === 0) return;
      const k = keyOf(mine[0]);
      if (seenRef.current.has(k)) return;
      seenRef.current.add(k);
      setLines((prev) => {
        const next = [...prev, mine[0]];
        return next.length > maxLines ? next.slice(next.length - maxLines) : next;
      });
    });
    return unsub;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [subscribe, slug, sessionId, maxLines]);

  const timeline = useMemo(() => summarizeStageTimeline(lines), [lines]);
  const liveStage = timeline.stages.find((s) => s.status === "running") ?? null;

  // Per-stage latency in seconds for the telemetry charts.
  const latencySeconds = useMemo(
    () => timeline.durations.map((d) => d / 1000),
    [timeline.durations],
  );
  const stageLabels = useMemo(
    () =>
      timeline.stages
        .filter((s) => s.durationMs != null)
        .map((s) => s.name),
    [timeline.stages],
  );

  return (
    <section
      className={[
        "rounded-2xl border border-accent-line/50 bg-[linear-gradient(180deg,rgba(12,16,22,0.96),rgba(8,11,16,0.99))] shadow-[0_18px_48px_rgba(0,0,0,0.32)] overflow-hidden",
        className ?? "",
      ].join(" ")}
      aria-label="Live build console"
    >
      <ConsoleHeader
        status={status}
        liveStageName={liveStage?.name ?? null}
        total={timeline.total}
        completed={timeline.completed}
        failed={timeline.failed}
        lineCount={lines.length}
      />

      {(latencySeconds.length > 0 || timeline.total > 0) && (
        <StageTelemetry
          latencySeconds={latencySeconds}
          labels={stageLabels}
          totalStages={timeline.total}
        />
      )}

      <ConsoleLog lines={lines} status={status} />
    </section>
  );
}

// ----------------------------------------------------------------------------
// Header — connection state + scanline + live stage
// ----------------------------------------------------------------------------

function ConsoleHeader({
  status,
  liveStageName,
  total,
  completed,
  failed,
  lineCount,
}: {
  status: string;
  liveStageName: string | null;
  total: number;
  completed: number;
  failed: number;
  lineCount: number;
}) {
  const live = status === "open";
  const dotColor = live
    ? "bg-status-green"
    : status === "connecting"
      ? "bg-status-yellow"
      : "bg-status-red";
  const stateLabel = live ? "live" : status === "connecting" ? "connecting" : "offline";

  return (
    <header className="relative flex flex-wrap items-center justify-between gap-3 border-b border-border bg-[linear-gradient(180deg,rgba(56,212,240,0.05),transparent)] px-4 py-3">
      {/* top accent edge; pulses while live, static under reduced-motion */}
      <span
        aria-hidden
        className={[
          "pointer-events-none absolute inset-x-0 top-0 h-px",
          "bg-[linear-gradient(90deg,transparent,var(--accent-glow),transparent)]",
          live ? "animate-pulse motion-reduce:animate-none" : "opacity-40",
        ].join(" ")}
      />
      <div className="flex min-w-0 items-center gap-2.5">
        <span
          aria-hidden
          className={[
            "inline-block h-2 w-2 rounded-full",
            dotColor,
            live ? "live-dot" : "",
          ].join(" ")}
        />
        <h3 className="text-[0.7rem] font-mono uppercase tracking-[0.18em] text-text-secondary">
          Build console
        </h3>
        <span
          className={[
            "rounded border px-1.5 py-0.5 text-[0.6rem] font-mono uppercase tracking-wider",
            live
              ? "border-status-green/30 bg-status-green/10 text-status-green"
              : status === "connecting"
                ? "border-status-yellow/30 bg-status-yellow/10 text-status-yellow"
                : "border-status-red/30 bg-status-red/10 text-status-red",
          ].join(" ")}
          role="status"
        >
          {stateLabel}
        </span>
        {liveStageName && (
          <span
            className="ml-1 inline-flex min-w-0 items-center gap-1.5 truncate font-mono text-xs text-accent"
            title={`Running: ${liveStageName}`}
          >
            <span className="text-text-dim">▸</span>
            <span className="truncate">{liveStageName}</span>
          </span>
        )}
      </div>

      <div className="flex shrink-0 items-center gap-2 font-mono text-[0.62rem] uppercase tracking-wider">
        <span className="rounded border border-border bg-bg-3/70 px-1.5 py-0.5 text-status-green">
          {completed} done
        </span>
        {failed > 0 && (
          <span className="rounded border border-status-red/30 bg-status-red/10 px-1.5 py-0.5 text-status-red">
            {failed} failed
          </span>
        )}
        <span className="rounded border border-border bg-bg-3/70 px-1.5 py-0.5 text-text-dim">
          {total} stages
        </span>
        <span className="rounded border border-border bg-bg-3/70 px-1.5 py-0.5 text-text-dim">
          {lineCount} lines
        </span>
      </div>
    </header>
  );
}

// ----------------------------------------------------------------------------
// Telemetry — real charts driven by live stage durations
// ----------------------------------------------------------------------------

function StageTelemetry({
  latencySeconds,
  labels,
  totalStages,
}: {
  latencySeconds: number[];
  labels: string[];
  totalStages: number;
}) {
  const measured = latencySeconds.length;
  const slowestIdx = useMemo(() => {
    if (latencySeconds.length === 0) return -1;
    let idx = 0;
    for (let i = 1; i < latencySeconds.length; i += 1) {
      if (latencySeconds[i] > latencySeconds[idx]) idx = i;
    }
    return idx;
  }, [latencySeconds]);

  const total = latencySeconds.reduce((a, b) => a + b, 0);
  const avg = measured > 0 ? total / measured : 0;

  return (
    <div className="grid gap-px border-b border-border bg-border/40 sm:grid-cols-[minmax(0,1fr)_auto]">
      <div className="bg-bg-1/60 px-4 py-3">
        <div className="mb-1.5 flex items-baseline justify-between gap-2">
          <span className="section-label">Stage latency</span>
          <span className="font-mono text-[0.6rem] text-text-dim">seconds</span>
        </div>
        {measured > 0 ? (
          <div className="text-amber">
            <MiniBars
              values={latencySeconds}
              labels={labels}
              height={44}
              highlightIndex={slowestIdx}
              aria-label={`Stage latency for ${measured} completed stage${measured === 1 ? "" : "s"}`}
              className="w-full"
            />
          </div>
        ) : (
          <p className="py-3 font-mono text-xs text-text-dim">
            Awaiting first completed stage…
          </p>
        )}
      </div>

      <div className="flex items-center gap-4 bg-bg-1/60 px-4 py-3">
        <Stat label="Measured" value={`${measured}/${totalStages}`} />
        <Stat label="Avg" value={fmtDuration(avg * 1000)} tone="accent" />
        <Stat label="Total" value={fmtDuration(total * 1000)} tone="amber" />
        {measured > 1 && (
          <div className="hidden text-accent sm:block" aria-hidden>
            <Sparkline
              values={latencySeconds}
              width={84}
              height={32}
              fill
              aria-label="Latency trend"
            />
          </div>
        )}
      </div>
    </div>
  );
}

function Stat({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "accent" | "amber";
}) {
  const toneCls =
    tone === "accent" ? "text-accent" : tone === "amber" ? "text-amber" : "text-text-primary";
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-[0.55rem] font-mono uppercase tracking-wider text-text-dim">
        {label}
      </span>
      <span className={`font-mono text-sm tabular-nums ${toneCls}`}>{value}</span>
    </div>
  );
}

// ----------------------------------------------------------------------------
// Log — newest at bottom, auto-scroll w/ pause-on-hover/scroll-up
// ----------------------------------------------------------------------------

function ConsoleLog({ lines, status }: { lines: SwarmEvent[]; status: string }) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const [paused, setPaused] = useState(false);
  const [hovering, setHovering] = useState(false);

  // Track whether the user has scrolled away from the bottom; if so, hold.
  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    setPaused(distanceFromBottom > 48);
  };

  // Auto-scroll to newest unless the user paused (scrolled up) or is hovering.
  useLayoutEffect(() => {
    if (paused || hovering) return;
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [lines.length, paused, hovering]);

  const empty = lines.length === 0;

  return (
    <div className="relative">
      <div
        ref={scrollRef}
        onScroll={onScroll}
        onMouseEnter={() => setHovering(true)}
        onMouseLeave={() => setHovering(false)}
        className="max-h-[52vh] min-h-[200px] overflow-y-auto px-1 py-1.5"
        role="log"
        aria-live="polite"
        aria-relevant="additions"
        aria-label="Build event log, newest at the bottom"
        tabIndex={0}
      >
        {empty ? (
          <EmptyState status={status} />
        ) : (
          <ol className="space-y-0.5">
            {lines.map((e, i) => (
              <ConsoleLine key={keyForRender(e, i)} e={e} />
            ))}
          </ol>
        )}
      </div>

      {(paused || hovering) && !empty && (
        <button
          type="button"
          onClick={() => {
            setPaused(false);
            setHovering(false);
            const el = scrollRef.current;
            if (el) el.scrollTop = el.scrollHeight;
          }}
          className="absolute bottom-3 right-3 flex items-center gap-1.5 rounded-full border border-accent-line bg-bg-2/90 px-3 py-1 text-[0.65rem] font-mono uppercase tracking-wider text-accent shadow-glow-sm backdrop-blur transition-colors hover:bg-accent-soft focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
        >
          <span aria-hidden>▾</span> Jump to latest
        </button>
      )}
    </div>
  );
}

function keyForRender(e: SwarmEvent, i: number): string {
  const cid = e.meta?.correlation_id;
  if (cid) return `cid:${cid}:${i}`;
  return `${i}:${e.ts ?? ""}:${e.event_type ?? ""}`;
}

function EmptyState({ status }: { status: string }) {
  const offline = status === "closed";
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-6 py-12 text-center">
      <span aria-hidden className="text-2xl text-text-dim">
        {offline ? "⚠" : "›_"}
      </span>
      <p className="font-mono text-sm text-text-secondary">
        {offline
          ? "Live stream offline — reconnecting."
          : "Listening for build events…"}
      </p>
      <p className="max-w-sm text-xs leading-5 text-text-dim">
        {offline
          ? "Stage transitions, agent thoughts, and LLM exchanges for this build will stream here once the connection recovers."
          : "Stage transitions, agent thoughts, and LLM exchanges will appear here as the swarm works."}
      </p>
    </div>
  );
}

// ----------------------------------------------------------------------------
// One console line
// ----------------------------------------------------------------------------

function ConsoleLine({ e }: { e: SwarmEvent }) {
  const tone = lineTone(e);
  const ts = parseConsoleTs(e.ts);
  const title = buildEventTitle(e);

  if (e.kind === "convo") {
    return <ConvoLine e={e} ts={ts} />;
  }

  const stageDuration =
    isStageEvent(e) && stagePhase(projectSubKind(e)) !== "start"
      ? e.meta?.payload?.duration_ms
      : null;

  return (
    <li className="group flex items-start gap-2.5 rounded px-2.5 py-1 transition-colors hover:bg-bg-3/60">
      <span className="mt-1.5 shrink-0 text-[0.58rem] font-mono tabular-nums text-text-dim">
        {ts ? ts.toLocaleTimeString([], { hour12: false }) : "--:--:--"}
      </span>
      <span aria-hidden className={`mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full ${TONE_DOT[tone]}`} />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
          <span className={`font-mono text-[0.78rem] leading-5 ${TONE_TEXT[tone]}`}>
            {title}
          </span>
          {e.from && (
            <span className="font-mono text-[0.62rem] text-text-dim">{e.from}</span>
          )}
          {typeof stageDuration === "number" && (
            <span className="rounded bg-bg-3 px-1 py-px font-mono text-[0.58rem] text-text-secondary">
              {fmtDuration(stageDuration)}
            </span>
          )}
        </div>
        <DetailLine e={e} />
      </div>
    </li>
  );
}

function DetailLine({ e }: { e: SwarmEvent }) {
  // Thought / learning lines benefit from a short payload preview.
  const preview =
    (typeof e.meta?.payload?.preview === "string" && e.meta.payload.preview) ||
    (typeof e.meta?.payload?.summary === "string" && e.meta.payload.summary) ||
    "";
  if (!preview) return null;
  return (
    <p className="mt-0.5 line-clamp-2 break-words font-mono text-[0.68rem] leading-4 text-text-dim">
      {preview}
    </p>
  );
}

// ----------------------------------------------------------------------------
// Collapsible LLM exchange card
// ----------------------------------------------------------------------------

function ConvoLine({ e, ts }: { e: SwarmEvent; ts: Date | null }) {
  const [open, setOpen] = useState(false);
  const model = e.meta?.model;
  const backend = e.meta?.backend;
  const duration = e.meta?.duration_ms;
  const prompt = (e.meta?.prompt || "").trim();
  const response = (e.meta?.response || "").trim();
  const hasBody = Boolean(prompt || response);

  return (
    <li className="rounded px-2.5 py-1">
      <button
        type="button"
        onClick={() => hasBody && setOpen((v) => !v)}
        aria-expanded={hasBody ? open : undefined}
        disabled={!hasBody}
        className={[
          "flex w-full items-start gap-2.5 rounded px-1 py-0.5 text-left transition-colors",
          hasBody
            ? "cursor-pointer hover:bg-bg-3/60 focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
            : "cursor-default",
        ].join(" ")}
      >
        <span className="mt-1.5 shrink-0 text-[0.58rem] font-mono tabular-nums text-text-dim">
          {ts ? ts.toLocaleTimeString([], { hour12: false }) : "--:--:--"}
        </span>
        <span aria-hidden className={`mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full ${TONE_DOT.convo}`} />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            {hasBody && (
              <span aria-hidden className="font-mono text-[0.6rem] text-text-dim">
                {open ? "▾" : "▸"}
              </span>
            )}
            <span className="font-mono text-[0.78rem] text-chrome">LLM exchange</span>
            {model && <Chip>{String(model)}</Chip>}
            {backend && backend !== model && <Chip tone="dim">{String(backend)}</Chip>}
            {typeof duration === "number" && (
              <Chip tone="accent">{fmtDuration(duration)}</Chip>
            )}
          </div>
          {!open && prompt && (
            <p className="mt-0.5 line-clamp-1 break-words font-mono text-[0.66rem] text-text-dim">
              {prompt}
            </p>
          )}
        </div>
      </button>

      {open && hasBody && (
        <div className="ml-8 mt-1 space-y-2 border-l border-border pl-3">
          {prompt && (
            <ExchangeBlock label="prompt" tone="amber" body={prompt} />
          )}
          {response && (
            <ExchangeBlock label="response" tone="accent" body={response} />
          )}
        </div>
      )}
    </li>
  );
}

function ExchangeBlock({
  label,
  tone,
  body,
}: {
  label: string;
  tone: "amber" | "accent";
  body: string;
}) {
  const toneCls = tone === "amber" ? "text-amber" : "text-accent";
  return (
    <div>
      <div className={`mb-0.5 font-mono text-[0.55rem] uppercase tracking-wider ${toneCls}`}>
        {label}
      </div>
      <pre className="max-h-48 overflow-y-auto whitespace-pre-wrap break-words rounded bg-bg-0/60 p-2 font-mono text-[0.66rem] leading-4 text-text-secondary">
        {body}
      </pre>
    </div>
  );
}

function Chip({
  children,
  tone,
}: {
  children: React.ReactNode;
  tone?: "accent" | "dim";
}) {
  const cls =
    tone === "accent"
      ? "border-accent-line bg-accent-soft text-accent"
      : tone === "dim"
        ? "border-border bg-bg-3 text-text-dim"
        : "border-border bg-bg-3 text-text-secondary";
  return (
    <span
      className={`rounded border px-1 py-px font-mono text-[0.55rem] uppercase tracking-wider ${cls}`}
    >
      {children}
    </span>
  );
}

export default BuildConsole;
