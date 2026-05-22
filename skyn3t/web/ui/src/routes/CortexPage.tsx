import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import {
  api,
  CortexComponentStatus,
  CortexStatus,
  Proposal,
} from "../api/client";

// Phrases that mean "build a separate program," not "patch SkyN3t
// itself." Cortex's feature_handler only knows how to patch existing
// repo files via CodeImproverAgent, so build-style ideas should go to
// Studio instead.
const BUILD_VERBS = [
  /\bbuild (me )?(a|an|the)?\b/i,
  /\bcreate (a|an|the)?\b/i,
  /\bmake (me )?(a|an|the)?\b/i,
  /\bgenerate (a|an|the)?\b/i,
  /\bscaffold\b/i,
  /\b(new|fresh) (project|app|program|cli|api|site|page|tool)\b/i,
  /\btodo app\b/i,
  /\blanding page\b/i,
  /\bweb (app|site)\b/i,
  /\bmobile app\b/i,
  /\bios app\b/i,
];

function looksLikeBuild(text: string): boolean {
  const t = text.toLowerCase();
  return BUILD_VERBS.some((re) => re.test(t));
}

// Cortex — the self-improvement inbox. Pending proposals up top
// (where the user decides), decided ones collapsed below for audit.
// Plus one form to file an idea.
export default function CortexPage() {
  const qc = useQueryClient();
  const [filter, setFilter] = useState<"pending" | "all">("pending");
  const [kindFilter, setKindFilter] = useState<"all" | "feature" | "ingest">("all");

  const { data, isLoading, error } = useQuery({
    queryKey: ["proposals", filter],
    queryFn: () =>
      api.proposals(filter === "pending" ? { status: "pending" } : undefined),
    refetchInterval: 8_000,
  });
  const cortexStatus = useQuery({
    queryKey: ["cortex-status"],
    queryFn: () => api.cortexStatus(),
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
  const visiblePending = useMemo(
    () =>
      grouped.pending.filter((p) => kindFilter === "all" || p.kind === kindFilter),
    [grouped.pending, kindFilter],
  );
  const pendingGroups = useMemo(() => {
    const groups = new Map<
      string,
      { key: string; kind: string; source: string; count: number; example: Proposal }
    >();
    for (const p of grouped.pending) {
      const key = `${p.kind}::${p.source ?? "unknown"}`;
      const current = groups.get(key);
      if (current) current.count += 1;
      else {
        groups.set(key, {
          key,
          kind: p.kind,
          source: p.source ?? "unknown",
          count: 1,
          example: p,
        });
      }
    }
    return Array.from(groups.values()).sort((a, b) => b.count - a.count);
  }, [grouped.pending]);

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
      <CortexStatusPanel
        data={cortexStatus.data}
        isLoading={cortexStatus.isLoading}
        error={cortexStatus.error}
      />

      {grouped.pending.length > 0 && (
        <PendingTriagePanel
          total={grouped.pending.length}
          kindFilter={kindFilter}
          onKindFilterChange={setKindFilter}
          groups={pendingGroups}
        />
      )}

      {isLoading && <p className="text-text-secondary">Loading…</p>}
      {error && (
        <p className="text-status-red text-sm">
          {error instanceof Error ? error.message : "load failed"}
        </p>
      )}

      <section className="space-y-3">
        <SectionTitle>
          Pending ({visiblePending.length}
          {kindFilter !== "all" ? ` / ${grouped.pending.length}` : ""})
        </SectionTitle>
        {visiblePending.length === 0 ? (
          <Empty>No pending proposals. The system is quiet.</Empty>
        ) : (
          visiblePending.map((p) => (
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

function PendingTriagePanel({
  total,
  kindFilter,
  onKindFilterChange,
  groups,
}: {
  total: number;
  kindFilter: "all" | "feature" | "ingest";
  onKindFilterChange: (value: "all" | "feature" | "ingest") => void;
  groups: Array<{ key: string; kind: string; source: string; count: number; example: Proposal }>;
}) {
  return (
    <section className="rounded-lg border border-border bg-bg-2 p-4 space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <SectionTitle>Pending triage</SectionTitle>
          <p className="text-xs text-text-secondary mt-1 max-w-3xl">
            These are review-gated proposals. Approve when you want the system to
            adopt the suggested change or learning topic; reject when it feels noisy,
            overfit, or irrelevant.
          </p>
        </div>
        <div className="flex bg-bg-3 border border-border rounded p-0.5 text-xs">
          {(["all", "feature", "ingest"] as const).map((value) => (
            <button
              key={value}
              onClick={() => onKindFilterChange(value)}
              className={[
                "px-3 py-1 rounded uppercase tracking-wider",
                kindFilter === value
                  ? "bg-accent-soft text-accent border border-accent-line"
                  : "text-text-secondary hover:text-text-primary",
              ].join(" ")}
            >
              {value} {value === "all" ? `(${total})` : ""}
            </button>
          ))}
        </div>
      </div>

      <div className="grid gap-3 md:grid-cols-2">
        {groups.map((group) => {
          const triage = proposalTriage(group.example);
          return (
            <div
              key={group.key}
              className="rounded border border-border bg-bg-3 p-3 space-y-2"
            >
              <div className="flex items-center justify-between gap-2">
                <div className="min-w-0">
                  <div className="text-sm font-medium truncate">
                    {group.kind} · {group.source}
                  </div>
                  <div className="text-[0.65rem] text-text-dim font-mono">
                    {group.count} pending
                  </div>
                </div>
                <span className="rounded border border-accent-line bg-accent-soft px-2 py-0.5 text-[0.65rem] uppercase tracking-wider text-accent">
                  {triage.label}
                </span>
              </div>
              <div className="text-xs text-text-secondary">
                {triage.approveWhen}
              </div>
              <div className="text-[0.65rem] text-text-dim">
                Example: {proposalSubject(group.example) || group.example.title || group.example.summary}
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}

function CortexStatusPanel({
  data,
  isLoading,
  error,
}: {
  data?: CortexStatus;
  isLoading: boolean;
  error?: unknown;
}) {
  if (isLoading && !data) {
    return (
      <section className="rounded-lg border border-border bg-bg-2 p-4">
        <SectionTitle>Cortex runtime</SectionTitle>
        <p className="text-text-secondary text-sm">Loading status…</p>
      </section>
    );
  }
  if (error) {
    return (
      <section className="rounded-lg border border-status-red/40 bg-status-red/10 p-4">
        <SectionTitle>Cortex runtime</SectionTitle>
        <p className="text-status-red text-sm">
          {error instanceof Error ? error.message : "status failed"}
        </p>
      </section>
    );
  }
  if (!data) return null;

  return (
    <section className="rounded-lg border border-border bg-bg-2 p-4 space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <SectionTitle>Cortex runtime</SectionTitle>
          <p className="text-xs text-text-secondary mt-1">
            Which self-improvement components are live, what proposal handlers are
            registered, and whether anything is currently broken.
          </p>
        </div>
        <div className="flex items-center gap-2 text-xs font-mono">
          <RuntimeBadge ok={data.running && data.booted}>
            {data.running && data.booted ? "live" : "degraded"}
          </RuntimeBadge>
          <span className="text-text-dim">
            handlers: {data.proposal_handlers.length}
          </span>
        </div>
      </div>

      <div className="grid gap-3 lg:grid-cols-[1.2fr,1fr]">
        <div className="space-y-3">
          <KvLabel>Components</KvLabel>
          <div className="grid gap-2 sm:grid-cols-2">
            {data.components.map((component) => (
              <ComponentCard key={component.name} component={component} />
            ))}
          </div>
        </div>

        <div className="space-y-3">
          <div>
            <KvLabel>Proposal handlers</KvLabel>
            <div className="flex flex-wrap gap-1.5">
              {data.proposal_handlers.length > 0 ? (
                data.proposal_handlers.map((kind) => (
                  <span
                    key={kind}
                    className="text-[0.65rem] px-1.5 py-0.5 rounded border border-accent-line text-accent font-mono"
                  >
                    {kind}
                  </span>
                ))
              ) : (
                <span className="text-xs text-status-yellow">No handlers registered.</span>
              )}
            </div>
          </div>

          <div>
            <KvLabel>Proposal counts</KvLabel>
            <div className="flex flex-wrap gap-2 text-xs">
              {Object.keys(data.proposal_counts).length > 0 ? (
                Object.entries(data.proposal_counts).map(([status, count]) => (
                  <span
                    key={status}
                    className="rounded border border-border bg-bg-3 px-2 py-1 font-mono"
                  >
                    {status}: {count}
                  </span>
                ))
              ) : (
                <span className="text-text-secondary">No proposals yet.</span>
              )}
            </div>
          </div>

          {data.warnings.length > 0 && (
            <div className="rounded border border-status-yellow/40 bg-status-yellow/10 p-3">
              <KvLabel>Warnings</KvLabel>
              <ul className="space-y-1 text-xs text-status-yellow">
                {data.warnings.map((warning, index) => (
                  <li key={`${warning}-${index}`}>• {warning}</li>
                ))}
              </ul>
            </div>
          )}

          {data.recent_failures.length > 0 && (
            <div className="rounded border border-status-red/40 bg-status-red/10 p-3">
              <KvLabel>Recent failed proposals</KvLabel>
              <ul className="space-y-2 text-xs">
                {data.recent_failures.map((failure) => (
                  <li key={failure.id}>
                    <div className="font-mono text-status-red">
                      {failure.kind} · {failure.id}
                    </div>
                    <div className="text-text-secondary">{failure.title}</div>
                    {failure.error && (
                      <div className="text-status-red whitespace-pre-wrap break-words">
                        {failure.error}
                      </div>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </div>
    </section>
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
  const [debounced, setDebounced] = useState("");
  const isBuild = looksLikeBuild(idea);

  // Debounce preview lookups so we're not hammering the backend per
  // keystroke. 350ms is the sweet spot — quick enough to feel live,
  // slow enough that bursts of typing don't fire 30 requests.
  useEffect(() => {
    const t = window.setTimeout(() => setDebounced(idea.trim()), 350);
    return () => window.clearTimeout(t);
  }, [idea]);

  const preview = useQuery({
    queryKey: ["feature_preview", debounced],
    queryFn: () => api.previewFeatureIdea(debounced),
    enabled: debounced.length >= 8 && !isBuild,
    staleTime: 30_000,
  });

  const file = useMutation({
    mutationFn: (text: string) => api.fileFeatureIdea(text),
    onSuccess: (res) => {
      if (res.ok) {
        setIdea("");
        setDebounced("");
        onFiled();
      }
    },
  });

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        const t = idea.trim();
        if (t && !isBuild) file.mutate(t);
      }}
      className="rounded-lg border border-border bg-bg-2 p-4 space-y-3"
    >
      <SectionTitle>File a self-improvement</SectionTitle>
      <p className="text-xs text-text-secondary -mt-1">
        Describe a change to <em>SkyN3t itself</em>. Be specific — mention the
        area (planner, cortex, studio, rag, an agent name) or a file path so
        the system can find where to patch. To build a fresh program instead,
        use{" "}
        <Link to="/studio" className="text-accent hover:underline">
          Studio
        </Link>
        .
      </p>
      <textarea
        value={idea}
        onChange={(e) => setIdea(e.target.value)}
        rows={5}
        placeholder={`Examples:

• Make the planner agent pick faster when there are >10 templates.
• In skyn3t/cortex/handlers.py, when feature_handler can't infer a target file, suggest the closest match instead of failing.
• Add a 'priority' field to TaskRequest so the orchestrator can reorder queues.
• Cache /api/rag/recent for 30 seconds to reduce vector store load.`}
        className="w-full bg-bg-3 border border-border rounded px-3 py-2 text-sm outline-none focus:border-accent resize-y font-mono"
      />

      {isBuild && (
        <div className="rounded border border-status-yellow/40 bg-status-yellow/10 p-3 text-xs">
          <div className="font-medium text-status-yellow mb-1">
            <i className="fa-solid fa-lightbulb mr-1.5" />
            This looks like a build, not a self-improvement.
          </div>
          <p className="text-text-secondary">
            Cortex only patches existing files in this repo. To scaffold a new
            program from a brief, go to{" "}
            <Link to="/studio" className="text-accent hover:underline font-medium">
              Studio
            </Link>{" "}
            and pick a template.
          </p>
        </div>
      )}

      {!isBuild && debounced.length >= 8 && (
        <PreviewPanel
          isLoading={preview.isFetching}
          data={preview.data}
          error={preview.error}
        />
      )}

      <div className="flex items-center gap-3 pt-1 border-t border-border">
        <button
          type="submit"
          disabled={!idea.trim() || isBuild || file.isPending}
          className="rounded bg-accent text-bg-0 text-sm font-medium px-3 py-1.5 disabled:opacity-60"
        >
          {file.isPending ? "Filing…" : "File proposal"}
        </button>
        {isBuild && (
          <Link
            to="/studio"
            className="rounded bg-accent-soft border border-accent-line text-accent text-sm font-medium px-3 py-1.5"
          >
            <i className="fa-solid fa-arrow-right mr-1" />
            Open Studio
          </Link>
        )}
        {preview.data?.next_action?.kind === "blocked" && !isBuild && (
          <span className="text-xs text-status-yellow">
            <i className="fa-solid fa-circle-exclamation mr-1" />
            No target file inferred yet — filing now will likely fail on approval.
          </span>
        )}
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

// Live preview of what filing the idea would do. Shows the inferred
// target file, capability areas the idea touches, and the planned
// execution narrative.
function PreviewPanel({
  isLoading,
  data,
  error,
}: {
  isLoading: boolean;
  data?: any;
  error?: unknown;
}) {
  if (isLoading && !data) {
    return (
      <div className="rounded border border-border bg-bg-3 p-3 text-xs text-text-dim">
        <i className="fa-solid fa-arrows-rotate animate-spin mr-1.5" />
        Previewing...
      </div>
    );
  }
  if (error) {
    return (
      <div className="rounded border border-status-red/40 bg-status-red/10 p-3 text-xs text-status-red">
        {error instanceof Error ? error.message : "preview failed"}
      </div>
    );
  }
  if (!data) return null;

  const blocked = data.next_action?.kind === "blocked";
  const accentBorder = blocked
    ? "border-status-yellow/40 bg-status-yellow/5"
    : "border-accent-line bg-accent-soft";

  return (
    <div className={`rounded-lg border p-3 space-y-3 ${accentBorder}`}>
      <div className="text-xs uppercase tracking-wider text-text-secondary font-medium flex items-center gap-2">
        <i className="fa-solid fa-eye" />
        Preview — what would happen on approval
      </div>

      {/* Target file */}
      <div>
        <KvLabel>Target file</KvLabel>
        {data.target_file ? (
          <code className="text-xs font-mono text-accent break-all">
            {data.target_file}
          </code>
        ) : (
          <span className="text-xs text-status-yellow">
            None inferred — be more specific (mention an area or file path).
          </span>
        )}
      </div>

      {/* Planned execution */}
      <div>
        <KvLabel>Planned execution</KvLabel>
        <p className="text-xs text-text-primary whitespace-pre-wrap">
          {data.next_action?.summary}
        </p>
        {data.next_action?.agent && (
          <div className="text-[0.65rem] text-text-dim font-mono mt-1">
            agent: {data.next_action.agent} · kind: {data.next_action.kind}
          </div>
        )}
      </div>

      {/* Capability areas */}
      {Array.isArray(data.capability_hits) && data.capability_hits.length > 0 && (
        <div>
          <KvLabel>Capability areas touched</KvLabel>
          <ul className="space-y-2">
            {data.capability_hits.map((hit: any, i: number) => (
              <li
                key={i}
                className="text-xs border border-border rounded bg-bg-2 p-2"
              >
                <div className="flex flex-wrap gap-1 mb-1">
                  {hit.keywords.map((k: string) => (
                    <span
                      key={k}
                      className="text-[0.6rem] px-1.5 py-0.5 rounded border border-accent-line text-accent font-mono"
                    >
                      {k}
                    </span>
                  ))}
                </div>
                <ul className="font-mono text-[0.65rem] text-text-secondary space-y-0.5">
                  {hit.related_files.map((f: string) => (
                    <li key={f} className="truncate" title={f}>
                      · {f}
                    </li>
                  ))}
                </ul>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Keywords (collapsed) */}
      {Array.isArray(data.keywords) && data.keywords.length > 0 && (
        <div>
          <KvLabel>Extracted keywords</KvLabel>
          <div className="flex flex-wrap gap-1">
            {data.keywords.slice(0, 20).map((k: string) => (
              <span
                key={k}
                className="text-[0.6rem] px-1.5 py-0.5 rounded border border-border bg-bg-3 text-text-secondary font-mono"
              >
                {k}
              </span>
            ))}
            {data.keywords.length > 20 && (
              <span className="text-[0.6rem] text-text-dim">
                + {data.keywords.length - 20} more
              </span>
            )}
          </div>
        </div>
      )}

      <p className="text-[0.65rem] text-text-dim italic">
        This is a preview only. Nothing is filed until you click "File proposal".
      </p>
    </div>
  );
}

function KvLabel({ children }: { children: React.ReactNode }) {
  return (
    <div className="text-[0.65rem] uppercase tracking-wider text-text-secondary mb-1">
      {children}
    </div>
  );
}

function ComponentCard({ component }: { component: CortexComponentStatus }) {
  const healthy = component.started && !component.error;
  return (
    <div className="rounded border border-border bg-bg-3 p-3 space-y-2">
      <div className="flex items-center justify-between gap-2">
        <div>
          <div className="text-sm font-medium">{component.name}</div>
          <div className="text-[0.65rem] font-mono text-text-dim">
            {component.class_name || "component"}
          </div>
        </div>
        <RuntimeBadge ok={healthy}>
          {healthy ? "started" : "down"}
        </RuntimeBadge>
      </div>
      {component.subscriptions.length > 0 && (
        <div>
          <KvLabel>Subscriptions</KvLabel>
          <div className="text-[0.65rem] font-mono text-text-secondary break-words">
            {component.subscriptions.join(", ")}
          </div>
        </div>
      )}
      {(component.creates_proposals.length > 0 || component.handles_proposals.length > 0) && (
        <div className="text-[0.65rem] text-text-secondary space-y-1">
          {component.creates_proposals.length > 0 && (
            <div>
              creates:{" "}
              <span className="font-mono">
                {component.creates_proposals.join(", ")}
              </span>
            </div>
          )}
          {component.handles_proposals.length > 0 && (
            <div>
              handles:{" "}
              <span className="font-mono">
                {component.handles_proposals.join(", ")}
              </span>
            </div>
          )}
        </div>
      )}
      {component.error && (
        <div className="text-xs text-status-red whitespace-pre-wrap break-words">
          {component.error}
        </div>
      )}
    </div>
  );
}

function RuntimeBadge({
  ok,
  children,
}: {
  ok: boolean;
  children: React.ReactNode;
}) {
  return (
    <span
      className={[
        "rounded-full px-2 py-0.5 text-[0.65rem] uppercase tracking-wider border",
        ok
          ? "border-status-green/40 bg-status-green/10 text-status-green"
          : "border-status-yellow/40 bg-status-yellow/10 text-status-yellow",
      ].join(" ")}
    >
      {children}
    </span>
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
  const triage = proposalTriage(p);
  const subject = proposalSubject(p);
  const highlights = proposalHighlights(p);

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
            {subject && (
              <span className="text-[0.65rem] px-1.5 py-0.5 rounded border border-border bg-bg-3 text-text-secondary font-mono shrink-0">
                {subject}
              </span>
            )}
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

      {!compact && (
        <div className="px-3 pb-3 space-y-2">
          <div className="rounded border border-border bg-bg-3 p-2.5 space-y-1">
            <div className="flex items-center gap-2 text-[0.65rem] uppercase tracking-wider">
              <span className="text-accent font-medium">{triage.label}</span>
              <span className="text-text-dim">approve when</span>
            </div>
            <p className="text-xs text-text-secondary">{triage.approveWhen}</p>
            <p className="text-[0.65rem] text-text-dim">{triage.rejectWhen}</p>
          </div>
          {highlights.length > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {highlights.map((item) => (
                <span
                  key={item}
                  className="text-[0.65rem] px-1.5 py-0.5 rounded border border-border bg-bg-3 text-text-secondary font-mono"
                >
                  {item}
                </span>
              ))}
            </div>
          )}
        </div>
      )}

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

function proposalSubject(p: Proposal): string | null {
  const payload = p.payload ?? {};
  if (p.kind === "feature" && typeof payload.stack === "string" && payload.stack) {
    return `stack:${payload.stack}`;
  }
  if (p.kind === "ingest" && typeof payload.topic === "string" && payload.topic) {
    return `topic:${payload.topic}`;
  }
  if (typeof payload.kind === "string" && payload.kind) {
    return payload.kind;
  }
  return null;
}

function proposalHighlights(p: Proposal): string[] {
  const payload = p.payload ?? {};
  const out: string[] = [];
  if (typeof payload.winner_samples === "number" && typeof payload.winner_success_rate === "number") {
    out.push(`winner ${payload.winner_samples} @ ${Math.round(payload.winner_success_rate * 100)}%`);
  }
  if (typeof payload.loser_samples === "number" && typeof payload.loser_success_rate === "number") {
    out.push(`loser ${payload.loser_samples} @ ${Math.round(payload.loser_success_rate * 100)}%`);
  }
  if (typeof payload.limit === "number") {
    out.push(`limit ${payload.limit}`);
  }
  if (typeof payload.mode === "string" && payload.mode) {
    out.push(`mode ${payload.mode}`);
  }
  return out;
}

function proposalTriage(p: Proposal): {
  label: string;
  approveWhen: string;
  rejectWhen: string;
} {
  const payload = p.payload ?? {};
  if (p.kind === "feature" && payload.kind === "build_pattern_bias") {
    return {
      label: "template default change",
      approveWhen:
        "Approve if you want future scaffolds on this stack to follow the higher-success pattern shown here.",
      rejectWhen:
        "Reject if the sample looks too narrow, the winning shape is obviously incomplete, or you do not want this template behavior to become the new default.",
    };
  }
  if (p.kind === "ingest") {
    return {
      label: "external learning topic",
      approveWhen:
        "Approve if this topic is relevant to SkyN3t and you want it to ingest outside material for future memory/skill synthesis.",
      rejectWhen:
        "Reject if the topic is noisy, duplicate, off-mission, or you do not want external docs on this area right now.",
    };
  }
  return {
    label: "manual review",
    approveWhen:
      "Approve if the summary clearly describes a useful change to SkyN3t itself and the detail matches what you want applied.",
    rejectWhen:
      "Reject if the proposal is vague, duplicated, overfit, or not something you want the system acting on.",
  };
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
