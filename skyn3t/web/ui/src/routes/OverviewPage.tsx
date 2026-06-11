import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";

// The landing view. Three tiles for now: agent count, running tasks,
// recent activity. Each tile is its own query so a slow one doesn't
// block the rest of the page.
export default function OverviewPage() {
  return (
    <div className="space-y-6">
      <Header />
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <StatusTile />
        <AgentsTile />
        <BuildPatternsTile />
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <AutonomousTile />
        <OpenRouterTile />
      </div>
    </div>
  );
}

function Header() {
  return (
    <div>
      <h1 className="display text-4xl">
        <span className="text-accent">Overview</span>
      </h1>
      <p className="text-text-secondary text-sm mt-1">
        Live system state. The backend pushes counts; this view polls every 10s.
      </p>
    </div>
  );
}

function Tile({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-border bg-bg-2 p-5">
      <div className="text-xs uppercase tracking-wider text-text-secondary font-medium mb-3">
        {title}
      </div>
      {children}
    </div>
  );
}

function StatusTile() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["status"],
    queryFn: api.status,
    refetchInterval: 10_000,
  });
  if (isLoading) return <Tile title="Status"><Skeleton /></Tile>;
  if (error)     return <Tile title="Status"><ErrText error={error} /></Tile>;
  const totalAgents =
    data?.total_agents ??
    (data?.agents && typeof data.agents === "object"
      ? Object.keys(data.agents).length
      : 0);
  return (
    <Tile title="Status">
      <div className="space-y-1">
        <Stat label="Agents registered" value={totalAgents} />
        <Stat label="Running tasks"     value={data?.running_tasks ?? 0} />
      </div>
    </Tile>
  );
}

function AgentsTile() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["agents"],
    queryFn: api.agents,
    refetchInterval: 15_000,
  });
  if (isLoading) return <Tile title="Agents"><Skeleton /></Tile>;
  if (error)     return <Tile title="Agents"><ErrText error={error} /></Tile>;
  const total = data?.length ?? 0;
  const busy = (data ?? []).filter((a) => a.status === "busy").length;
  return (
    <Tile title="Agents">
      <div className="space-y-1">
        <Stat label="Total"  value={total} />
        <Stat label="Busy"   value={busy} />
      </div>
    </Tile>
  );
}

function BuildPatternsTile() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["build_patterns_summary"],
    queryFn: () => api.buildPatterns(),
    refetchInterval: 60_000,
  });
  if (isLoading) return <Tile title="Build patterns"><Skeleton /></Tile>;
  if (error)     return <Tile title="Build patterns"><ErrText error={error} /></Tile>;
  const s = data?.summary ?? {};
  return (
    <Tile title="Build patterns">
      <div className="space-y-1">
        <Stat label="Stacks tracked" value={s.stacks_tracked ?? 0} />
        <Stat label="Shapes tracked" value={s.shapes_tracked ?? 0} />
        <Stat label="Total success"  value={s.total_success ?? 0} />
        <Stat label="Total failure"  value={s.total_failure ?? 0} />
      </div>
    </Tile>
  );
}

function AutonomousTile() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["autonomous_status"],
    queryFn: api.autonomousStatus,
    refetchInterval: 15_000,
    retry: false,
  });
  if (isLoading) return <Tile title="Autonomous loop"><Skeleton /></Tile>;
  if (error)     return <Tile title="Autonomous loop"><ErrText error={error} /></Tile>;
  if (data?.available === false) {
    return (
      <Tile title="Autonomous loop">
        <p className="text-text-secondary text-sm">Coordinator not available on this backend.</p>
      </Tile>
    );
  }
  const proofLabel =
    data?.last_proof_ok == null
      ? "—"
      : data.last_proof_ok
        ? "passed"
        : "failed";
  return (
    <Tile title="Autonomous loop">
      <div className="space-y-1">
        <Stat
          label="Learning"
          value={data?.autonomous_learning ? "on" : "off"}
        />
        <Stat
          label="Auto-builds"
          value={data?.autonomous_builds ? "on" : "off"}
        />
        <Stat
          label="Builds today"
          value={`${data?.daily_builds ?? 0}/${data?.daily_cap ?? 0}`}
        />
        <Stat label="Queue" value={data?.queue_depth ?? 0} />
        <Stat label="Last proof" value={proofLabel} />
        {data?.last_build_slug && (
          <p className="text-text-dim text-xs font-mono truncate pt-1">
            {data.last_build_slug}
          </p>
        )}
        {data?.last_proof_summary && data.last_proof_ok === false && (
          <p className="text-status-red text-xs truncate">{data.last_proof_summary}</p>
        )}
      </div>
    </Tile>
  );
}

function OpenRouterTile() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["openrouter_models"],
    queryFn: () => api.openrouterModels(false),
    refetchInterval: 120_000,
    retry: false,
  });
  if (isLoading) return <Tile title="OpenRouter catalog"><Skeleton /></Tile>;
  if (error)     return <Tile title="OpenRouter catalog"><ErrText error={error} /></Tile>;
  const count = data?.model_count ?? (data as { count?: number })?.count ?? data?.models?.length ?? 0;
  const synced = data?.synced_at
    ? new Date(data.synced_at * 1000).toLocaleString()
    : "never";
  return (
    <Tile title="OpenRouter catalog">
      <div className="space-y-1">
        <Stat label="Models cached" value={count} />
        <Stat label="Sync" value={data?.sync_enabled ? "enabled" : "off"} />
        <Stat label="Stale" value={data?.stale ? "yes" : "no"} />
        <p className="text-text-dim text-xs pt-1">Last sync: {synced}</p>
      </div>
    </Tile>
  );
}

function Stat({ label, value }: { label: string; value: number | string }) {
  return (
    <div className="flex items-baseline justify-between">
      <span className="text-text-secondary text-sm">{label}</span>
      <span className="font-mono text-accent text-lg">{value}</span>
    </div>
  );
}

function Skeleton() {
  return <div className="h-12 rounded bg-bg-3 animate-pulse" />;
}

function ErrText({ error }: { error: unknown }) {
  return (
    <div className="text-status-red text-xs">
      {error instanceof Error ? error.message : "unknown error"}
    </div>
  );
}
