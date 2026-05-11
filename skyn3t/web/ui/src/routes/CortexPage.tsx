import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, Proposal } from "../api/client";

// Cortex — the self-improvement inbox. Pending proposals up top
// (where the user decides), decided ones collapsed below for audit.
// Plus one form to file an idea.
export default function CortexPage() {
  const qc = useQueryClient();
  const [filter, setFilter] = useState<"pending" | "all">("pending");

  const { data, isLoading, error } = useQuery({
    queryKey: ["proposals", filter],
    queryFn: () =>
      api.proposals(filter === "pending" ? { status: "pending" } : undefined),
    refetchInterval: 8_000,
  });

  const invalidate = () => qc.invalidateQueries({ queryKey: ["proposals"] });

  const grouped = useMemo(() => {
    const pending: Proposal[] = [];
    const decided: Proposal[] = [];
    for (const p of data ?? []) {
      if (p.status === "pending" || p.status === "applying") pending.push(p);
      else decided.push(p);
    }
    pending.sort((a, b) => b.created_at - a.created_at);
    decided.sort((a, b) => (b.decided_at ?? 0) - (a.decided_at ?? 0));
    return { pending, decided };
  }, [data]);

  return (
    <div className="space-y-6">
      <header className="flex items-baseline justify-between gap-4">
        <div>
          <h1 className="display text-4xl">
            <span className="text-accent">Cortex</span>
          </h1>
          <p className="text-text-secondary text-sm mt-1">
            Self-improvement proposals — what the system wants to change about
            itself, plus ideas you file. Approve, reject, or watch them apply.
          </p>
        </div>
        <FilterToggle value={filter} onChange={setFilter} />
      </header>

      <FileIdeaForm onFiled={invalidate} />

      {isLoading && <p className="text-text-secondary">Loading…</p>}
      {error && (
        <p className="text-status-red text-sm">
          {error instanceof Error ? error.message : "load failed"}
        </p>
      )}

      <section className="space-y-3">
        <SectionTitle>
          Pending ({grouped.pending.length})
        </SectionTitle>
        {grouped.pending.length === 0 ? (
          <Empty>No pending proposals. The system is quiet.</Empty>
        ) : (
          grouped.pending.map((p) => (
            <ProposalCard key={p.id} p={p} onChanged={invalidate} />
          ))
        )}
      </section>

      {filter === "all" && (
        <section className="space-y-3">
          <SectionTitle>Decided ({grouped.decided.length})</SectionTitle>
          {grouped.decided.length === 0 ? (
            <Empty>Nothing decided yet.</Empty>
          ) : (
            grouped.decided.map((p) => (
              <ProposalCard key={p.id} p={p} onChanged={invalidate} compact />
            ))
          )}
        </section>
      )}
    </div>
  );
}

function FilterToggle({
  value,
  onChange,
}: {
  value: "pending" | "all";
  onChange: (v: "pending" | "all") => void;
}) {
  return (
    <div className="flex bg-bg-2 border border-border rounded p-0.5 text-xs">
      {(["pending", "all"] as const).map((v) => (
        <button
          key={v}
          onClick={() => onChange(v)}
          className={[
            "px-3 py-1 rounded uppercase tracking-wider",
            value === v
              ? "bg-accent-soft text-accent border border-accent-line"
              : "text-text-secondary hover:text-text-primary",
          ].join(" ")}
        >
          {v}
        </button>
      ))}
    </div>
  );
}

function FileIdeaForm({ onFiled }: { onFiled: () => void }) {
  const [idea, setIdea] = useState("");
  const file = useMutation({
    mutationFn: (text: string) => api.fileFeatureIdea(text),
    onSuccess: (res) => {
      if (res.ok) {
        setIdea("");
        onFiled();
      }
    },
  });

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        const t = idea.trim();
        if (t) file.mutate(t);
      }}
      className="rounded-lg border border-border bg-bg-2 p-4 space-y-2"
    >
      <SectionTitle>File an idea</SectionTitle>
      <textarea
        value={idea}
        onChange={(e) => setIdea(e.target.value)}
        rows={2}
        placeholder="What should the system build, fix, or change about itself?"
        className="w-full bg-bg-3 border border-border rounded px-3 py-2 text-sm outline-none focus:border-accent resize-y"
      />
      <div className="flex items-center gap-3">
        <button
          type="submit"
          disabled={!idea.trim() || file.isPending}
          className="rounded bg-accent text-bg-0 text-sm font-medium px-3 py-1.5 disabled:opacity-60"
        >
          {file.isPending ? "Filing…" : "File idea"}
        </button>
        {file.data?.error && (
          <span className="text-status-red text-xs">{file.data.error}</span>
        )}
        {file.data?.ok && file.data.proposal_id && (
          <span className="text-status-green text-xs font-mono">
            filed · {file.data.proposal_id}
          </span>
        )}
      </div>
    </form>
  );
}

function ProposalCard({
  p,
  onChanged,
  compact,
}: {
  p: Proposal;
  onChanged: () => void;
  compact?: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const [rejectOpen, setRejectOpen] = useState(false);
  const [reason, setReason] = useState("");

  const approve = useMutation({
    mutationFn: () => api.approveProposal(p.id),
    onSuccess: onChanged,
  });
  const reject = useMutation({
    mutationFn: (r: string) => api.rejectProposal(p.id, r),
    onSuccess: () => {
      setRejectOpen(false);
      setReason("");
      onChanged();
    },
  });

  const decisionable = p.status === "pending";

  return (
    <article
      className={[
        "rounded-lg border bg-bg-2",
        decisionable ? "border-accent-line" : "border-border",
      ].join(" ")}
    >
      <header className="flex items-baseline justify-between gap-3 p-3 min-w-0">
        <div className="min-w-0">
          <div className="flex items-center gap-2 min-w-0">
            <span className="text-[0.65rem] font-mono uppercase tracking-wider text-text-dim shrink-0">
              {p.kind}
            </span>
            <h3
              className="font-medium truncate"
              title={p.title || p.summary}
            >
              {p.title || p.summary || p.id}
            </h3>
          </div>
          {!compact && p.summary && (
            <p className="text-sm text-text-secondary mt-1">{p.summary}</p>
          )}
          <div className="text-[0.65rem] text-text-dim font-mono mt-1 truncate">
            {new Date(p.created_at * 1000).toLocaleString()}
            {p.source && ` · ${p.source}`}
            {p.origin && ` · ${p.origin}`}
            {` · ${p.id}`}
          </div>
        </div>
        <StatusPill status={p.status} />
      </header>

      {p.detail && (
        <div className="px-3 pb-2">
          <button
            onClick={() => setExpanded(!expanded)}
            className="text-xs text-text-secondary hover:text-text-primary"
          >
            <i
              className={`fa-solid fa-caret-${expanded ? "down" : "right"} mr-1`}
            />
            {expanded ? "Hide" : "Show"} detail
          </button>
          {expanded && (
            <pre className="mt-2 text-xs font-mono whitespace-pre-wrap break-words bg-bg-3 border border-border rounded p-3 max-h-96 overflow-y-auto">
              {p.detail}
            </pre>
          )}
        </div>
      )}

      {p.error && (
        <p className="px-3 pb-2 text-xs text-status-red whitespace-pre-wrap break-words">
          {p.error}
        </p>
      )}

      {decisionable && (
        <footer className="border-t border-border px-3 py-2 flex items-center gap-2 flex-wrap">
          <button
            onClick={() => approve.mutate()}
            disabled={approve.isPending}
            className="rounded bg-accent text-bg-0 text-xs font-medium px-3 py-1 disabled:opacity-60"
          >
            <i className="fa-solid fa-check mr-1" />
            {approve.isPending ? "Approving…" : "Approve"}
          </button>
          <button
            onClick={() => setRejectOpen(!rejectOpen)}
            className="rounded border border-status-red/40 text-status-red text-xs px-3 py-1 hover:bg-status-red/10"
          >
            <i className="fa-solid fa-xmark mr-1" />
            Reject
          </button>
          {approve.data?.error && (
            <span className="text-status-red text-xs">{approve.data.error}</span>
          )}
          {rejectOpen && (
            <div className="flex items-center gap-2 ml-auto">
              <input
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                placeholder="Reason (optional)"
                className="bg-bg-3 border border-border rounded px-2 py-1 text-xs"
              />
              <button
                onClick={() => reject.mutate(reason)}
                disabled={reject.isPending}
                className="rounded bg-status-red/30 text-status-red text-xs px-2 py-1"
              >
                Confirm
              </button>
            </div>
          )}
        </footer>
      )}
    </article>
  );
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <h3 className="text-xs uppercase tracking-wider text-text-secondary font-medium">
      {children}
    </h3>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return (
    <p className="text-sm text-text-dim border border-dashed border-border rounded-lg px-4 py-6 text-center">
      {children}
    </p>
  );
}

function StatusPill({ status }: { status: string }) {
  const s = (status ?? "unknown").toString();
  const color =
    s === "approved" || s === "applied" || s === "ok"
      ? "bg-status-green/20 text-status-green border-status-green/30"
      : s === "applying" || s === "pending"
        ? "bg-accent-soft text-accent border-accent-line"
        : s === "rejected" || s === "failed"
          ? "bg-status-red/20 text-status-red border-status-red/30"
          : "bg-bg-3 text-text-dim border-border";
  return (
    <span
      className={`inline-block px-2 py-0.5 rounded text-[0.65rem] uppercase tracking-wider border shrink-0 ${color}`}
    >
      {s}
    </span>
  );
}
