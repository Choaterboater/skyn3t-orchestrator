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
