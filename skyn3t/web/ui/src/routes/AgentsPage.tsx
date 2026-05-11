import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";

// Agent registry view. Each row truncates long names cleanly — fixes
// the squished-column complaint that drove this rebuild.
export default function AgentsPage() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["agents"],
    queryFn: api.agents,
    refetchInterval: 15_000,
  });
  return (
    <div className="space-y-6">
      <header>
        <h1 className="display text-4xl">
          <span className="text-accent">Agents</span>
        </h1>
        <p className="text-text-secondary text-sm mt-1">
          Every registered agent — their backend, status, and queue depth.
        </p>
      </header>

      {isLoading && <p className="text-text-secondary">Loading…</p>}
      {error && (
        <p className="text-status-red text-sm">
          {error instanceof Error ? error.message : "load failed"}
        </p>
      )}
      {data && data.length === 0 && (
        <p className="text-text-secondary">No agents registered yet.</p>
      )}
      {data && data.length > 0 && (
        <div className="rounded-lg border border-border bg-bg-2 overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-bg-3 text-text-secondary text-xs uppercase tracking-wider">
              <tr>
                <Th>Name</Th>
                <Th>Type</Th>
                <Th>Provider</Th>
                <Th align="right">Status</Th>
                <Th align="right">Queue</Th>
                <Th align="right">Errors</Th>
              </tr>
            </thead>
            <tbody>
              {data.map((a) => (
                <tr key={a.name} className="border-t border-border hover:bg-accent-soft">
                  <Td truncate>{a.name}</Td>
                  <Td>{a.agent_type ?? "—"}</Td>
                  <Td>{a.provider ?? "—"}</Td>
                  <Td align="right">
                    <StatusPill status={a.status ?? "unknown"} />
                  </Td>
                  <Td align="right" mono>{a.queue_depth ?? 0}</Td>
                  <Td align="right" mono>{a.recent_errors ?? 0}</Td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function Th({
  children,
  align = "left",
}: {
  children: React.ReactNode;
  align?: "left" | "right";
}) {
  return (
    <th
      className={`px-4 py-2 font-medium ${align === "right" ? "text-right" : "text-left"}`}
    >
      {children}
    </th>
  );
}

function Td({
  children,
  align = "left",
  mono,
  truncate,
}: {
  children: React.ReactNode;
  align?: "left" | "right";
  mono?: boolean;
  truncate?: boolean;
}) {
  return (
    <td
      className={[
        "px-4 py-2",
        align === "right" ? "text-right" : "text-left",
        mono ? "font-mono" : "",
        truncate ? "max-w-xs truncate" : "",
      ].join(" ")}
      title={truncate && typeof children === "string" ? children : undefined}
    >
      {children}
    </td>
  );
}

function StatusPill({ status }: { status: string }) {
  const color =
    status === "idle"
      ? "bg-status-green/20 text-status-green border-status-green/30"
      : status === "busy"
        ? "bg-accent-soft text-accent border-accent-line"
        : status === "error"
          ? "bg-status-red/20 text-status-red border-status-red/30"
          : "bg-bg-3 text-text-dim border-border";
  return (
    <span
      className={`inline-block px-2 py-0.5 rounded text-xs uppercase tracking-wider border ${color}`}
    >
      {status}
    </span>
  );
}
