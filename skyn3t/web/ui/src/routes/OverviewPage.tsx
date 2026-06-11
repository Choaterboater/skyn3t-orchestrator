import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import FleetGrid, { FleetSlotList } from "../components/FleetGrid";
import MetricCard, { StatRow } from "../components/MetricCard";
import { PageHeader, PanelCard, PanelHeader } from "../components/Panel";
import StatusPill from "../components/StatusPill";
import Sparkline, { MiniBars } from "../components/Sparkline";
import { useSwarm } from "../context/SwarmProvider";

/* ------------------------------------------------------------------ *
 * Pure helpers (exported for the colocated vitest test)
 * ------------------------------------------------------------------ */

/** Parse a SwarmEvent timestamp defensively. Server sends ISO strings, but
 * snapshot backfill may carry legacy numeric epoch-seconds. Returns epoch ms,
 * or null when unparseable. */
export function parseEventTs(ts: unknown): number | null {
  if (typeof ts === "number" && Number.isFinite(ts)) {
    // Heuristic: seconds vs ms. Anything below ~1e12 is treated as seconds.
    return ts < 1e12 ? ts * 1000 : ts;
  }
  if (typeof ts === "string" && ts) {
    const n = Number(ts);
    if (Number.isFinite(n) && /^\d+(\.\d+)?$/.test(ts.trim())) {
      return n < 1e12 ? n * 1000 : n;
    }
    const parsed = Date.parse(ts);
    return Number.isNaN(parsed) ? null : parsed;
  }
  return null;
}

/**
 * Bucket recent events into events-per-second over a rolling window ending at
 * `now`. Returns oldest->newest counts of length `buckets`. Pure & total: empty
 * input yields a flat zero series; never throws.
 */
export function bucketEventsPerSecond(
  events: Array<{ ts: unknown }>,
  opts?: { now?: number; buckets?: number; bucketMs?: number },
): number[] {
  const buckets = Math.max(1, opts?.buckets ?? 40);
  const bucketMs = Math.max(1, opts?.bucketMs ?? 1000);
  const now = opts?.now ?? Date.now();
  const out = new Array<number>(buckets).fill(0);
  const windowStart = now - buckets * bucketMs;
  for (const e of events) {
    const ms = parseEventTs(e?.ts);
    if (ms == null) continue;
    if (ms < windowStart || ms > now) continue;
    // newest bucket is index buckets-1
    let idx = buckets - 1 - Math.floor((now - ms) / bucketMs);
    if (idx < 0) idx = 0;
    if (idx > buckets - 1) idx = buckets - 1;
    out[idx] += 1;
  }
  return out;
}

/* ------------------------------------------------------------------ *
 * Reduced-motion (JS mirror of the CSS media query)
 * ------------------------------------------------------------------ */
function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(() => {
    if (typeof window === "undefined" || !window.matchMedia) return false;
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  });
  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    const onChange = () => setReduced(mq.matches);
    mq.addEventListener?.("change", onChange);
    return () => mq.removeEventListener?.("change", onChange);
  }, []);
  return reduced;
}

/**
 * Re-render at `intervalMs` only while live activity is recent (so the EKG can
 * scroll), then idle out to spare the CPU. Returns a monotonically increasing
 * tick the consumer can read to recompute time-based views.
 */
function useLiveTicker(active: boolean, intervalMs = 1000): number {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (!active) return;
    const id = window.setInterval(() => setTick((t) => (t + 1) % 1_000_000), intervalMs);
    return () => window.clearInterval(id);
  }, [active, intervalMs]);
  return tick;
}

/** Flash a transient highlight whenever `signal` changes (e.g. an event count
 * or lastEventAt). Honors reduced motion by returning false (static). */
function useFlashOnChange(signal: number | null | undefined, reduced: boolean, holdMs = 650): boolean {
  const [flash, setFlash] = useState(false);
  const prev = useRef(signal);
  useEffect(() => {
    if (signal === prev.current) return;
    prev.current = signal;
    if (reduced) return;
    setFlash(true);
    const id = window.setTimeout(() => setFlash(false), holdMs);
    return () => window.clearTimeout(id);
  }, [signal, reduced, holdMs]);
  return flash;
}

export default function OverviewPage() {
  return (
    <div className="space-y-6">
      <PageHeader
        title="Overview"
        subtitle="Mission control for the autonomous fleet. Live cortex over a 10–15s poll floor."
      />
      <HeroStatusStrip />
      <div className="grid grid-cols-1 xl:grid-cols-12 gap-4">
        <div className="xl:col-span-7 space-y-4">
          <FleetPanel />
          <AutonomyPanel />
        </div>
        <div className="xl:col-span-5 space-y-4">
          <CadencePanel />
          <ImprovementPanel />
          <OpenRouterPanel />
          <QuickLinksPanel />
        </div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * Hero strip — polled truth, animated by the live swarm
 * ------------------------------------------------------------------ */
function HeroStatusStrip() {
  const reduced = useReducedMotion();
  const swarm = useSwarm();
  const status = useQuery({ queryKey: ["status"], queryFn: api.status, refetchInterval: 10_000 });
  const fleet = useQuery({
    queryKey: ["fleet_status"],
    queryFn: api.fleetStatus,
    refetchInterval: 10_000,
    retry: false,
  });
  const improvement = useQuery({
    queryKey: ["improvement_status"],
    queryFn: api.improvementStatus,
    refetchInterval: 15_000,
    retry: false,
  });
  const openrouter = useQuery({
    queryKey: ["openrouter_models"],
    queryFn: () => api.openrouterModels(false),
    refetchInterval: 120_000,
    retry: false,
  });

  const totalAgents =
    status.data?.total_agents ??
    (status.data?.agents && typeof status.data.agents === "object"
      ? Object.keys(status.data.agents).length
      : 0);

  const fleetSlots = fleet.data?.slots ?? [];
  const fleetBusy = fleetSlots.filter((s) => s.state !== "idle").length;
  const fleetSize = fleet.data?.fleet_size ?? fleet.data?.configured_size ?? 20;
  const fleetRunning = fleet.data?.running && fleet.data?.available !== false;

  const tickAgo = fmtAgo(improvement.data?.last_tick_at);
  const evolutionCount =
    improvement.data?.model_evolutions_total ??
    (openrouter.data?.evolution?.runs_total as number | undefined) ??
    0;

  const serverOk = !status.error && status.data;
  const loading = status.isLoading || fleet.isLoading;

  // Live signals: total events fuels the activity flash; convo/task counts
  // brighten the relevant metrics on real traffic.
  const totalEvents = useMemo(
    () => Object.values(swarm.counts).reduce((a, b) => a + b, 0),
    [swarm.counts],
  );
  const taskEvents = (swarm.counts.task ?? 0) + (swarm.counts.stage ?? 0);
  const activityFlash = useFlashOnChange(totalEvents, reduced);
  const fleetFlash = useFlashOnChange(taskEvents, reduced);

  return (
    <div className="hero-strip">
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 divide-y sm:divide-y-0 divide-border/60">
        <HeroMetric
          label="Server"
          value={loading ? "…" : serverOk ? "online" : "offline"}
          sub={`${totalAgents} agents · ${status.data?.running_tasks ?? 0} tasks`}
          tone={serverOk ? "green" : "red"}
          pulse={!!serverOk}
        />
        <HeroMetric
          label="Fleet"
          value={
            fleet.data?.available === false
              ? "idle"
              : fleetRunning
                ? `${fleetBusy}/${fleetSize}`
                : "standby"
          }
          sub={
            fleetRunning
              ? `${fleet.data?.active_builds ?? 0} building · queue ${fleet.data?.queue_depth ?? 0}`
              : "Set SKYN3T_AGENT_FLEET_SIZE"
          }
          tone={fleetBusy > 0 ? "cyan" : "neutral"}
          pulse={fleetBusy > 0}
          flash={fleetFlash}
        />
        <HeroMetric
          label="Improvement"
          value={
            improvement.data?.available === false
              ? "off"
              : improvement.data?.running
                ? "ticking"
                : improvement.data?.enabled
                  ? "armed"
                  : "off"
          }
          sub={
            improvement.data?.available === false
              ? "flywheel unavailable"
              : `last tick ${tickAgo} · ${improvement.data?.ticks_total ?? 0} total`
          }
          tone={improvement.data?.running ? "amber" : "neutral"}
          pulse={!!improvement.data?.running}
        />
        <HeroMetric
          label="Live cortex"
          value={swarm.status === "open" ? totalEvents : swarm.status === "connecting" ? "…" : "off"}
          sub={
            swarm.status === "open"
              ? `${liveAgo(swarm.lastEventAt)} · ${Object.keys(swarm.counts).length} kinds`
              : swarm.status === "connecting"
                ? "linking to /ws/swarm"
                : "stream closed"
          }
          tone="cyan"
          pulse={swarm.status === "open"}
          flash={activityFlash}
        />
        <HeroMetric
          label="Model evolution"
          value={evolutionCount}
          sub={
            openrouter.data?.stale
              ? "catalog stale — sync pending"
              : `${openrouter.data?.model_count ?? 0} models cached`
          }
          tone="cyan"
        />
      </div>
    </div>
  );
}

function HeroMetric({
  label,
  value,
  sub,
  tone = "neutral",
  pulse,
  flash,
}: {
  label: string;
  value: string | number;
  sub?: string;
  tone?: "green" | "cyan" | "amber" | "red" | "neutral";
  pulse?: boolean;
  flash?: boolean;
}) {
  const valueColor =
    tone === "green"
      ? "text-status-green"
      : tone === "red"
        ? "text-status-red"
        : tone === "amber"
          ? "text-amber"
          : tone === "cyan"
            ? "text-accent"
            : "text-text-primary";

  return (
    <div
      className={[
        "hero-metric transition-[background-color,box-shadow] duration-500",
        flash ? (tone === "amber" ? "bg-amber-soft" : "bg-accent-soft glow-accent") : "",
      ].join(" ")}
    >
      <div className="section-label flex items-center gap-1.5">
        {pulse && <span className="live-dot" />}
        {label}
      </div>
      <div className={["font-mono text-xl sm:text-2xl font-medium tabular-nums", valueColor].join(" ")}>
        {value}
      </div>
      {sub && <div className="text-[0.65rem] text-text-dim font-mono truncate">{sub}</div>}
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * Cadence panel — the EKG. Pure liveness layer; derives entirely from the
 * SwarmProvider ring buffer. Resilient to an empty/offline buffer.
 * ------------------------------------------------------------------ */
function CadencePanel() {
  const reduced = useReducedMotion();
  const swarm = useSwarm();

  // Only spin the ticker while the stream is live and recently active.
  const recentlyLive =
    swarm.status === "open" &&
    swarm.lastEventAt != null &&
    Date.now() - swarm.lastEventAt < 60_000;
  const tick = useLiveTicker(!reduced && recentlyLive, 1000);

  const BUCKETS = 48;
  const series = useMemo(
    () => bucketEventsPerSecond(swarm.events, { buckets: BUCKETS, bucketMs: 1000 }),
    // tick forces the rolling window to advance; events drives content.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [swarm.events, tick],
  );

  const peak = Math.max(1, ...series);
  const totalInWindow = series.reduce((a, b) => a + b, 0);
  const nowRate = series[series.length - 1] ?? 0;

  // Per-kind mix over the buffer — small bar chart of where activity lives.
  const kindMix = useMemo(() => topKinds(swarm.counts, 6), [swarm.counts]);

  const flatline = totalInWindow === 0;

  return (
    <PanelCard>
      <PanelHeader
        title="Live cadence"
        icon="fa-solid fa-wave-square"
        description="Events-per-second over /ws/swarm. The pulse of the cortex."
        actions={<ConnPill status={swarm.status} reduced={reduced} />}
      />
      <div className="p-4 space-y-4">
        <div
          className={[
            "rounded-md border px-3 pt-3 pb-2 relative overflow-hidden transition-colors duration-500",
            !reduced && !flatline
              ? "border-accent-line bg-accent-soft/30"
              : "border-border bg-bg-3/40",
          ].join(" ")}
        >
          <div className="flex items-baseline justify-between mb-1">
            <span className="section-label">EKG · events/s</span>
            <span className="font-mono text-xs text-accent tabular-nums">
              {nowRate}
              <span className="text-text-dim">/s</span>
            </span>
          </div>
          <div className="text-accent">
            <Sparkline
              values={series}
              width={320}
              height={56}
              strokeWidth={1.75}
              fill
              min={0}
              max={peak}
              className="w-full"
              aria-label={`Event cadence: ${totalInWindow} events in the last ${BUCKETS} seconds, currently ${nowRate} per second`}
            />
          </div>
          {flatline && (
            <div className="absolute inset-0 flex items-center justify-center pointer-events-none">
              <span className="text-[0.65rem] uppercase tracking-[0.16em] text-text-dim">
                {swarm.status === "open" ? "awaiting signal" : "stream offline"}
              </span>
            </div>
          )}
        </div>

        <div className="grid grid-cols-3 gap-3">
          <MetricCard label="Window /48s" value={totalInWindow} icon="fa-solid fa-gauge" className="!p-3" />
          <MetricCard label="Peak/s" value={peak === 1 && flatline ? 0 : peak} accent="amber" className="!p-3" />
          <MetricCard
            label="Last"
            value={swarm.lastEventAt ? liveAgo(swarm.lastEventAt) : "—"}
            className="!p-3"
          />
        </div>

        {kindMix.length > 0 ? (
          <div>
            <div className="section-label mb-2">Activity mix</div>
            <div className="text-accent">
              <MiniBars
                values={kindMix.map((k) => k.count)}
                labels={kindMix.map((k) => k.kind)}
                height={44}
                highlightIndex={0}
                aria-label={`Event volume by kind: ${kindMix
                  .map((k) => `${k.kind} ${k.count}`)
                  .join(", ")}`}
                className="w-full"
              />
            </div>
            <div className="flex flex-wrap gap-x-3 gap-y-1 mt-2 text-[0.6rem] font-mono text-text-dim">
              {kindMix.map((k) => (
                <span key={k.kind}>
                  {k.kind} <span className="text-text-secondary">{k.count}</span>
                </span>
              ))}
            </div>
          </div>
        ) : (
          <p className="text-text-dim text-xs">No events observed since mount.</p>
        )}
      </div>
    </PanelCard>
  );
}

function ConnPill({
  status,
  reduced,
}: {
  status: "connecting" | "open" | "closed";
  reduced: boolean;
}) {
  if (status === "open") return <StatusPill status="running" label="live" pulse={!reduced} />;
  if (status === "connecting") return <StatusPill status="pending" label="linking" />;
  return <StatusPill status="offline" label="offline" />;
}

/* ------------------------------------------------------------------ *
 * Fleet panel — adds a token-burn trend; FleetGrid handles live flashes.
 * ------------------------------------------------------------------ */
function FleetPanel() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["fleet_status"],
    queryFn: api.fleetStatus,
    refetchInterval: 10_000,
    retry: false,
  });
  const usage = useQuery({
    queryKey: ["usage_agents"],
    queryFn: api.usagePerAgent,
    refetchInterval: 30_000,
    retry: false,
  });

  const fleetSize = data?.fleet_size ?? data?.configured_size ?? 20;
  const slots = data?.slots ?? [];

  // Token burn per agent (top 12 by total) -> MiniBars
  const burn = useMemo(() => {
    const rows = [...(usage.data ?? [])]
      .sort((a, b) => (b.total_tokens ?? 0) - (a.total_tokens ?? 0))
      .slice(0, 12);
    return rows;
  }, [usage.data]);

  return (
    <PanelCard>
      <PanelHeader
        title="Agent fleet"
        icon="fa-solid fa-sitemap"
        description="Build grid with live cyan flashes on task/stage events."
        actions={
          data?.backpressure ? (
            <StatusPill status="pending" label={data.backpressure} />
          ) : data?.running ? (
            <StatusPill status="running" label="live" pulse />
          ) : null
        }
      />
      <div className="p-4 space-y-4">
        {isLoading && <SkeletonBlock />}
        {error && <ErrText error={error} />}
        {data?.available === false || !data?.running ? (
          <p className="text-text-secondary text-sm">
            Fleet idle — configure{" "}
            <code className="font-mono text-xs bg-bg-3 px-1 rounded">SKYN3T_AGENT_FLEET_SIZE</code>{" "}
            to spin up parallel builders.
          </p>
        ) : (
          <>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
              <MetricCard
                label="Busy slots"
                value={`${slots.filter((s) => s.state !== "idle").length}/${fleetSize}`}
                accent="cyan"
                className="!p-3"
              />
              <MetricCard
                label="Building"
                value={data.active_builds ?? slots.filter((s) => s.state === "building").length}
                accent="cyan"
                className="!p-3"
              />
              <MetricCard
                label="Learning"
                value={data.active_learning ?? slots.filter((s) => s.state === "learning").length}
                accent="amber"
                className="!p-3"
              />
              <MetricCard
                label="Builds today"
                value={`${data.daily_builds ?? 0}/${data.daily_cap ?? 0}`}
                accent="amber"
                className="!p-3"
              />
            </div>
            <FleetGrid slots={slots} fleetSize={fleetSize} />
            {burn.length > 0 && (
              <div className="rounded-md border border-border bg-bg-3/30 p-3">
                <div className="flex items-center justify-between mb-2">
                  <span className="section-label">Token burn · top agents</span>
                  <span className="text-[0.6rem] font-mono text-text-dim">total tokens</span>
                </div>
                <div className="text-amber">
                  <MiniBars
                    values={burn.map((r) => r.total_tokens ?? 0)}
                    labels={burn.map((r) => r.agent)}
                    height={48}
                    highlightIndex={0}
                    aria-label={`Token burn by agent: ${burn
                      .map((r) => `${r.agent} ${r.total_tokens}`)
                      .join(", ")}`}
                    className="w-full"
                  />
                </div>
              </div>
            )}
            <FleetSlotList slots={slots} />
          </>
        )}
      </div>
    </PanelCard>
  );
}

function AutonomyPanel() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["autonomous_status"],
    queryFn: api.autonomousStatus,
    refetchInterval: 15_000,
    retry: false,
  });

  const proofLabel =
    data?.last_proof_ok == null ? "—" : data.last_proof_ok ? "passed" : "failed";

  return (
    <PanelCard>
      <PanelHeader
        title="Autonomous loop"
        icon="fa-solid fa-infinity"
        description="Coordinator-driven learning, proof runs, and daily build caps."
      />
      <div className="p-4">
        {isLoading && <SkeletonBlock />}
        {error && <ErrText error={error} />}
        {data?.available === false ? (
          <p className="text-text-secondary text-sm">Coordinator not available on this backend.</p>
        ) : (
          <div className="grid sm:grid-cols-2 gap-4">
            <div className="space-y-0">
              <StatRow label="Learning" value={data?.autonomous_learning ? "on" : "off"} />
              <StatRow label="Auto-builds" value={data?.autonomous_builds ? "on" : "off"} />
              <StatRow
                label="Builds today"
                value={`${data?.daily_builds ?? 0}/${data?.daily_cap ?? 0}`}
              />
              <StatRow label="Queue depth" value={data?.queue_depth ?? 0} />
            </div>
            <div className="space-y-0">
              <StatRow label="Last proof" value={proofLabel} />
              <StatRow
                label="Spend today"
                value={
                  data?.daily_spend_usd != null
                    ? `$${data.daily_spend_usd.toFixed(2)} / $${data.daily_budget_usd ?? 0}`
                    : "—"
                }
              />
              {data?.last_build_slug && (
                <p className="text-text-dim text-xs font-mono truncate pt-2">
                  last: {data.last_build_slug}
                </p>
              )}
              {data?.last_proof_summary && data.last_proof_ok === false && (
                <p className="text-status-red text-xs truncate pt-1">{data.last_proof_summary}</p>
              )}
            </div>
          </div>
        )}
      </div>
    </PanelCard>
  );
}

function ImprovementPanel() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["improvement_status"],
    queryFn: api.improvementStatus,
    refetchInterval: 15_000,
    retry: false,
  });

  return (
    <PanelCard>
      <PanelHeader
        title="Improvement flywheel"
        icon="fa-solid fa-arrows-spin"
        description="Never-stop loop: competitive practice, proof retries, cheaper routing."
        actions={
          data?.running ? <StatusPill status="running" label="ticking" pulse /> : null
        }
      />
      <div className="p-4">
        {isLoading && <SkeletonBlock />}
        {error && <ErrText error={error} />}
        {data?.available === false ? (
          <p className="text-text-secondary text-sm">Continuous improvement engine unavailable.</p>
        ) : (
          <div className="space-y-0">
            <StatRow label="Enabled" value={data?.enabled ? "yes" : "no"} />
            <StatRow label="Ticks total" value={data?.ticks_total ?? 0} />
            <StatRow label="Builds today" value={data?.builds_today ?? 0} />
            <StatRow
              label="Competitive practice"
              value={`${data?.competitive_practice_today ?? 0}/${
                (data as { daily_competitive_cap?: number } | undefined)?.daily_competitive_cap ?? 0
              }`}
            />
            <StatRow label="Model evolutions" value={data?.model_evolutions_total ?? 0} />
            <StatRow label="Cheaper routing" value={data?.cheaper_routing_applied ?? 0} />
            <p className="text-text-dim text-xs font-mono pt-2">
              last tick {fmtAgo(data?.last_tick_at)}
            </p>
          </div>
        )}
      </div>
    </PanelCard>
  );
}

function OpenRouterPanel() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["openrouter_models"],
    queryFn: () => api.openrouterModels(false),
    refetchInterval: 120_000,
    retry: false,
  });

  const count = data?.model_count ?? data?.models?.length ?? 0;
  const synced = data?.synced_at
    ? new Date(data.synced_at * 1000).toLocaleString()
    : "never";
  const evolution = data?.evolution;

  return (
    <PanelCard>
      <PanelHeader
        title="OpenRouter catalog"
        icon="fa-solid fa-layer-group"
        description="Cached model catalog with optional evolution sync."
        actions={
          data?.stale ? (
            <StatusPill status="pending" label="stale" />
          ) : (
            <StatusPill status="idle" label="fresh" />
          )
        }
      />
      <div className="p-4">
        {isLoading && <SkeletonBlock />}
        {error && <ErrText error={error} />}
        {!isLoading && !error && (
          <>
            <div className="grid grid-cols-2 gap-3 mb-3">
              <MetricCard label="Models" value={count} accent="cyan" className="!p-3" />
              <MetricCard
                label="Evolution runs"
                value={evolution?.runs_total ?? "—"}
                accent="amber"
                className="!p-3"
              />
            </div>
            <div className="space-y-0">
              <StatRow label="Sync" value={data?.sync_enabled ? "enabled" : "off"} />
              <StatRow
                label="Promoted / demoted"
                value={
                  evolution
                    ? `${evolution.models_promoted ?? 0} / ${evolution.models_demoted ?? 0}`
                    : "—"
                }
              />
            </div>
            <p className="text-text-dim text-xs font-mono pt-2">Last sync: {synced}</p>
            {(data?.models ?? []).slice(0, 4).map((m) => (
              <div
                key={m.id ?? m.name}
                className="text-[0.65rem] font-mono text-text-secondary truncate border-t border-border/40 py-1"
              >
                {m.name ?? m.id}
              </div>
            ))}
          </>
        )}
      </div>
    </PanelCard>
  );
}

function QuickLinksPanel() {
  const links = [
    { to: "/agents", label: "Routing wizard", icon: "fa-solid fa-route" },
    { to: "/studio", label: "Studio builds", icon: "fa-solid fa-hammer" },
    { to: "/cortex", label: "Cortex proposals", icon: "fa-solid fa-brain" },
    { to: "/traces", label: "Traces", icon: "fa-solid fa-stethoscope" },
  ];

  return (
    <PanelCard>
      <PanelHeader title="Quick links" icon="fa-solid fa-bolt" />
      <div className="p-3 grid grid-cols-2 gap-2">
        {links.map((l) => (
          <Link
            key={l.to}
            to={l.to}
            className="flex items-center gap-2 rounded-md border border-border bg-bg-3/40 px-3 py-2 text-sm text-text-secondary hover:text-accent hover:border-accent-line transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
          >
            <i className={[l.icon, "text-accent text-xs"].join(" ")} />
            {l.label}
          </Link>
        ))}
      </div>
    </PanelCard>
  );
}

function SkeletonBlock() {
  return <div className="h-16 rounded bg-bg-3 animate-pulse" />;
}

function ErrText({ error }: { error: unknown }) {
  return (
    <div className="text-status-red text-xs">
      {error instanceof Error ? error.message : "unknown error"}
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * Small pure-ish utilities
 * ------------------------------------------------------------------ */
function topKinds(counts: Record<string, number>, limit: number): Array<{ kind: string; count: number }> {
  return Object.entries(counts)
    .filter(([, v]) => v > 0)
    .sort((a, b) => b[1] - a[1])
    .slice(0, limit)
    .map(([kind, count]) => ({ kind, count }));
}

function fmtAgo(ts?: number): string {
  if (!ts) return "never";
  const sec = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  return `${Math.floor(sec / 3600)}h ago`;
}

/** Like fmtAgo but for epoch-ms (lastEventAt is ms). */
function liveAgo(ms: number | null): string {
  if (ms == null) return "idle";
  const sec = Math.max(0, Math.floor((Date.now() - ms) / 1000));
  if (sec < 1) return "now";
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  return `${Math.floor(sec / 3600)}h ago`;
}
