import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import FleetGrid, { FleetSlotList } from "../components/FleetGrid";
import MetricCard, { StatRow } from "../components/MetricCard";
import { PageHeader, PanelCard, PanelHeader } from "../components/Panel";
import StatusPill from "../components/StatusPill";

export default function OverviewPage() {
  return (
    <div className="space-y-6">
      <PageHeader
        title="Overview"
        subtitle="Mission control for the autonomous fleet. Live state polls every 10–15s."
      />
      <HeroStatusStrip />
      <div className="grid grid-cols-1 xl:grid-cols-12 gap-4">
        <div className="xl:col-span-7 space-y-4">
          <FleetPanel />
          <AutonomyPanel />
        </div>
        <div className="xl:col-span-5 space-y-4">
          <ImprovementPanel />
          <OpenRouterPanel />
          <QuickLinksPanel />
        </div>
      </div>
    </div>
  );
}

function HeroStatusStrip() {
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
          label="Model evolution"
          value={evolutionCount}
          sub={
            openrouter.data?.stale
              ? "catalog stale — sync pending"
              : `${openrouter.data?.model_count ?? 0} models cached`
          }
          tone="cyan"
        />
        <HeroMetric
          label="Auto-builds"
          value={
            improvement.data?.autonomous_builds_enabled
              ? `${improvement.data?.builds_today ?? 0}`
              : "off"
          }
          sub={`queue ${improvement.data?.autonomous_queue_depth ?? 0}`}
          tone={improvement.data?.autonomous_builds_enabled ? "amber" : "neutral"}
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
}: {
  label: string;
  value: string | number;
  sub?: string;
  tone?: "green" | "cyan" | "amber" | "red" | "neutral";
  pulse?: boolean;
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
    <div className="hero-metric">
      <div className="section-label flex items-center gap-1.5">
        {pulse && <span className="live-dot" />}
        {label}
      </div>
      <div className={["font-mono text-xl sm:text-2xl font-medium", valueColor].join(" ")}>
        {value}
      </div>
      {sub && <div className="text-[0.65rem] text-text-dim font-mono truncate">{sub}</div>}
    </div>
  );
}

function FleetPanel() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["fleet_status"],
    queryFn: api.fleetStatus,
    refetchInterval: 10_000,
    retry: false,
  });

  const fleetSize = data?.fleet_size ?? data?.configured_size ?? 20;
  const slots = data?.slots ?? [];

  return (
    <PanelCard>
      <PanelHeader
        title="Agent fleet"
        icon="fa-solid fa-sitemap"
        description="Twenty-slot build grid. Cyan pulse marks active builders."
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
              value={`${data?.competitive_practice_today ?? 0}/${data?.daily_competitive_cap ?? 0}`}
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
            className="flex items-center gap-2 rounded-md border border-border bg-bg-3/40 px-3 py-2 text-sm text-text-secondary hover:text-accent hover:border-accent-line transition-colors"
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

function fmtAgo(ts?: number): string {
  if (!ts) return "never";
  const sec = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  return `${Math.floor(sec / 3600)}h ago`;
}
