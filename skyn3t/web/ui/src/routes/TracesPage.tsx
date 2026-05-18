import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";

// Recent finished spans, newest first. Click one to see attrs + status.
// This is a debugging view — not the kind of thing you stare at, but
// you want it when something is slow or failing.
type Span = {
  trace_id: string;
  span_id: string;
  name: string;
  start_ts?: number;
  end_ts?: number;
  duration_ms?: number;
  status?: string;
  attributes?: Record<string, any>;
  error?: string;
  parent_id?: string | null;
};

export default function TracesPage() {
  const [selected, setSelected] = useState<string | null>(null);
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["traces"],
    queryFn: () => api.traces(100),
    refetchInterval: 8_000,
  });

  const spans: Span[] = (data ?? []) as Span[];
  const sel = useMemo(
    () => spans.find((s) => s.span_id === selected) ?? null,
    [spans, selected],
  );

  return (
    <div className="space-y-6">
      <header className="flex items-baseline justify-between gap-4">
        <div>
          <h1 className="display text-4xl">
            <span className="text-accent">Traces</span>
          </h1>
          <p className="text-text-secondary text-sm mt-1">
            Recent finished spans from the in-process tracer.
          </p>
        </div>
        <button
          onClick={() => refetch()}
          className="rounded border border-border text-xs px-2 py-1 text-text-secondary hover:border-border-strong"
        >
          <i className="fa-solid fa-arrows-rotate mr-1" />
          Refresh
        </button>
      </header>

      {isLoading && <p className="text-text-secondary">Loading…</p>}
      {error && (
        <p className="text-status-red text-sm">
          {error instanceof Error ? error.message : "load failed"}
        </p>
      )}

      <div className="grid grid-cols-[minmax(0,1fr)_380px] gap-5">
        <div className="rounded-lg border border-border bg-bg-2 overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-bg-3 text-text-secondary text-xs uppercase tracking-wider">
              <tr>
                <th className="px-3 py-2 text-left">Name</th>
                <th className="px-3 py-2 text-right">Duration</th>
                <th className="px-3 py-2 text-right">Status</th>
              </tr>
            </thead>
            <tbody>
              {spans.length === 0 && (
                <tr>
                  <td colSpan={3} className="p-4 text-center text-text-dim text-sm">
                    No traces recorded yet.
                  </td>
                </tr>
              )}
              {spans.map((s) => (
                <tr
                  key={s.span_id}
                  onClick={() => setSelected(s.span_id)}
                  className={[
                    "border-t border-border cursor-pointer",
                    selected === s.span_id
                      ? "bg-accent-soft"
                      : "hover:bg-bg-3",
                  ].join(" ")}
                >
                  <td className="px-3 py-2 min-w-0">
                    <div className="font-mono text-xs truncate" title={s.name}>
                      {s.name}
                    </div>
                    <div className="text-[0.6rem] text-text-dim font-mono mt-0.5 truncate">
                      {s.span_id?.slice(0, 12)}
                      {s.parent_id ? ` ← ${s.parent_id.slice(0, 8)}` : ""}
                    </div>
                  </td>
                  <td className="px-3 py-2 text-right font-mono text-xs">
                    {formatDuration(s.duration_ms)}
                  </td>
                  <td className="px-3 py-2 text-right">
                    <StatusPill status={s.status} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <aside>
          {sel ? <SpanDetail s={sel} /> : (
            <div className="rounded-lg border border-dashed border-border p-5 text-sm text-text-dim text-center">
              Pick a span on the left.
            </div>
          )}
        </aside>
      </div>
    </div>
  );
}

function SpanDetail({ s }: { s: Span }) {
  return (
    <div className="rounded-lg border border-border bg-bg-2 p-3 space-y-3 max-h-[75vh] overflow-y-auto">
      <div>
        <div className="text-xs uppercase tracking-wider text-text-secondary">
          Span
        </div>
        <div className="font-mono text-sm break-all">{s.name}</div>
      </div>
      <Kv label="span_id" value={s.span_id} mono />
      <Kv label="trace_id" value={s.trace_id} mono />
      {s.parent_id && <Kv label="parent_id" value={s.parent_id} mono />}
      <Kv label="duration" value={formatDuration(s.duration_ms)} />
      <Kv label="status" value={s.status ?? "—"} />
      {s.start_ts && (
        <Kv
          label="started"
          value={new Date(s.start_ts * 1000).toLocaleString()}
        />
      )}
      {s.error && (
        <div>
          <div className="text-xs uppercase tracking-wider text-status-red mb-1">
            Error
          </div>
          <pre className="text-xs font-mono text-status-red whitespace-pre-wrap break-words bg-status-red/10 border border-status-red/30 rounded p-2">
            {s.error}
          </pre>
        </div>
      )}
      {s.attributes && Object.keys(s.attributes).length > 0 && (
        <div>
          <div className="text-xs uppercase tracking-wider text-text-secondary mb-1">
            Attributes
          </div>
          <pre className="text-xs font-mono whitespace-pre-wrap break-words bg-bg-3 border border-border rounded p-2 max-h-72 overflow-y-auto">
            {JSON.stringify(s.attributes, null, 2)}
          </pre>
        </div>
      )}
    </div>
  );
}

function Kv({
  label,
  value,
  mono,
}: {
  label: string;
  value: React.ReactNode;
  mono?: boolean;
}) {
  return (
    <div className="flex items-baseline justify-between gap-2 min-w-0">
      <span className="text-xs text-text-secondary uppercase tracking-wider shrink-0">
        {label}
      </span>
      <span
        className={`text-xs text-right truncate ${mono ? "font-mono" : ""}`}
        title={typeof value === "string" ? value : undefined}
      >
        {value}
      </span>
    </div>
  );
}

function StatusPill({ status }: { status?: string }) {
  const s = (status ?? "ok").toString().toLowerCase();
  const color =
    s === "ok" || s === "completed"
      ? "bg-status-green/20 text-status-green border-status-green/30"
      : s === "error" || s === "failed"
        ? "bg-status-red/20 text-status-red border-status-red/30"
        : "bg-bg-3 text-text-dim border-border";
  return (
    <span
      className={`inline-block px-2 py-0.5 rounded text-[0.6rem] uppercase tracking-wider border ${color}`}
    >
      {s}
    </span>
  );
}

function formatDuration(ms?: number): string {
  if (typeof ms !== "number" || !isFinite(ms)) return "—";
  if (ms < 1) return `${(ms * 1000).toFixed(0)}μs`;
  if (ms < 1000) return `${ms.toFixed(1)}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}
