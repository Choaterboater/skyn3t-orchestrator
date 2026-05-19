import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ProjectRow, Template } from "../api/client";

// Studio (build) page — the biggest single view in the old dashboard.
// Splits into three regions:
//   left rail  — project list, with truncation so long titles don't
//                squish neighbouring columns (this was the original
//                complaint that drove the rebuild)
//   right pane — either a "new project" form or the selected project's
//                stages + history + artifacts
export default function StudioPage() {
  const qc = useQueryClient();
  const [selected, setSelected] = useState<string | null>(null);
  const [mode, setMode] = useState<"detail" | "new">("new");

  const projects = useQuery({
    queryKey: ["studio_projects"],
    queryFn: api.projects,
    refetchInterval: 5_000,
    // The list polls every 5s, but if a fetch fails (backend restart)
    // we don't want the page to stay frozen on a stale empty result.
    refetchOnMount: "always",
    refetchOnWindowFocus: true,
  });

  // Per-project token usage rollup. Falls back gracefully if the
  // tracker endpoint isn't available (older backend).
  const usage = useQuery({
    queryKey: ["usage_projects"],
    queryFn: api.usagePerProject,
    refetchInterval: 8_000,
    retry: false,
  });
  const usageBySlug = new Map(
    (usage.data ?? []).map((u: any) => [u.slug, u]),
  );

  const detail = useQuery({
    queryKey: ["studio_project", selected],
    queryFn: () => api.project(selected as string),
    enabled: !!selected && mode === "detail",
    refetchInterval: 4_000,
  });

  function pickProject(slug: string) {
    setSelected(slug);
    setMode("detail");
  }

  function newProject() {
    setSelected(null);
    setMode("new");
  }

  const onStarted = (slug?: string) => {
    qc.invalidateQueries({ queryKey: ["studio_projects"] });
    if (slug) {
      setSelected(slug);
      setMode("detail");
    }
  };

  return (
    <div className="space-y-6">
      <header className="flex items-baseline justify-between gap-4">
        <div>
          <h1 className="display text-4xl">
            <span className="text-accent">Studio</span>
          </h1>
          <p className="text-text-secondary text-sm mt-1">
            Brief the swarm, watch it build, verify, and learn. Projects
            land in <code className="font-mono bg-bg-3 px-1 rounded">data/projects/</code>.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => projects.refetch()}
            disabled={projects.isFetching}
            title="Refresh project list"
            className="rounded border border-border text-xs px-2 py-1.5 text-text-secondary hover:border-border-strong disabled:opacity-60"
          >
            <i
              className={`fa-solid fa-arrows-rotate ${
                projects.isFetching ? "animate-spin" : ""
              }`}
            />
          </button>
          <button
            onClick={newProject}
            className="rounded bg-accent text-bg-0 text-sm font-medium px-3 py-1.5"
          >
            <i className="fa-solid fa-plus mr-1.5" />
            New project
          </button>
        </div>
      </header>

      <div className="grid grid-cols-[300px_minmax(0,1fr)] gap-5">
        <ProjectList
          projects={projects.data ?? []}
          isLoading={projects.isLoading}
          error={projects.error}
          selected={selected}
          onPick={pickProject}
          usageBySlug={usageBySlug}
        />
        <div className="min-w-0">
          {mode === "new" && <NewProjectForm onStarted={onStarted} />}
          {mode === "detail" && selected && (
            <ProjectDetailView
              slug={selected}
              isLoading={detail.isLoading}
              error={detail.error}
              data={detail.data}
              onDeleted={() => {
                setSelected(null);
                setMode("new");
                qc.invalidateQueries({ queryKey: ["studio_projects"] });
              }}
            />
          )}
        </div>
      </div>
    </div>
  );
}

function ProjectList({
  projects,
  isLoading,
  error,
  selected,
  onPick,
  usageBySlug,
}: {
  projects: ProjectRow[];
  isLoading: boolean;
  error: unknown;
  selected: string | null;
  onPick: (slug: string) => void;
  usageBySlug: Map<string, { total_tokens: number; calls: number }>;
}) {
  const sorted = useMemo(
    () =>
      [...projects].sort((a, b) => {
        const ts = (p: ProjectRow) =>
          p.started_at ?? p.created_at ?? p.completed_at ?? 0;
        return ts(b) - ts(a);
      }),
    [projects],
  );
  return (
    <aside className="rounded-lg border border-border bg-bg-2 max-h-[75vh] overflow-y-auto">
      <div className="px-3 py-2 text-xs uppercase tracking-wider text-text-secondary border-b border-border bg-bg-3">
        Projects {projects.length > 0 && `(${projects.length})`}
      </div>
      {isLoading && (
        <p className="text-text-secondary text-sm p-4">Loading…</p>
      )}
      {error && (
        <p className="text-status-red text-sm p-4">
          {error instanceof Error ? error.message : "load failed"}
        </p>
      )}
      {!isLoading && sorted.length === 0 && (
        <p className="text-text-secondary text-sm p-4">
          No projects yet. Brief the swarm on the right.
        </p>
      )}
      <ul>
        {sorted.map((p) => {
          const active = p.slug === selected;
          return (
            <li key={p.slug}>
              <button
                onClick={() => onPick(p.slug)}
                className={[
                  "w-full text-left px-3 py-2 border-b border-border block min-w-0",
                  active
                    ? "bg-accent-soft border-l-2 border-l-accent"
                    : "hover:bg-bg-3",
                ].join(" ")}
              >
                <div className="flex items-center justify-between gap-2 min-w-0">
                  <span
                    className="truncate font-medium text-sm"
                    title={p.title || p.slug}
                  >
                    {p.title || p.slug}
                  </span>
                  <StatusPill status={p.status} />
                </div>
                <div className="text-[0.65rem] text-text-dim font-mono mt-1 truncate flex items-center gap-2">
                  <span className="truncate">{p.template ?? "—"} · {p.slug}</span>
                  {usageBySlug.get(p.slug) && (
                    <span
                      className="shrink-0 text-accent"
                      title={`${usageBySlug.get(p.slug)!.calls} LLM calls`}
                    >
                      {fmtTokensCompact(usageBySlug.get(p.slug)!.total_tokens)}
                    </span>
                  )}
                </div>
                {p.brief && (
                  <div
                    className="text-xs text-text-secondary mt-1 line-clamp-2"
                    title={p.brief}
                  >
                    {p.brief}
                  </div>
                )}
              </button>
            </li>
          );
        })}
      </ul>
    </aside>
  );
}

function ProjectDetailView({
  slug,
  isLoading,
  error,
  data,
  onDeleted,
}: {
  slug: string;
  isLoading: boolean;
  error: unknown;
  data: any;
  onDeleted: () => void;
}) {
  const del = useMutation({
    mutationFn: () => api.deleteProject(slug),
    onSuccess: onDeleted,
  });

  if (isLoading) {
    return <p className="text-text-secondary">Loading project…</p>;
  }
  if (error) {
    return (
      <p className="text-status-red text-sm">
        {error instanceof Error ? error.message : "load failed"}
      </p>
    );
  }
  if (!data) {
    return <p className="text-text-secondary">Project not found.</p>;
  }

  return (
    <div className="space-y-5 min-w-0">
      <div className="flex items-baseline justify-between gap-3">
        <div className="min-w-0">
          <h2 className="text-2xl font-semibold truncate" title={data.title || slug}>
            {data.title || slug}
          </h2>
          <div className="text-xs text-text-dim font-mono mt-0.5 truncate">
            {data.template ?? "—"} · {slug}
          </div>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <StatusPill status={data.status} />
          <button
            onClick={() => {
              if (
                window.confirm(
                  `Delete project ${slug}? This removes data/projects/${slug}.`,
                )
              ) {
                del.mutate();
              }
            }}
            disabled={del.isPending}
            className="text-xs px-2 py-1 rounded border border-status-red/40 text-status-red hover:bg-status-red/10 disabled:opacity-60"
          >
            <i className="fa-solid fa-trash mr-1" />
            Delete
          </button>
        </div>
      </div>

      {data.brief && (
        <section>
          <SectionTitle>Brief</SectionTitle>
          <p className="text-sm text-text-primary bg-bg-2 border border-border rounded-lg p-3 whitespace-pre-wrap break-words">
            {data.brief}
          </p>
        </section>
      )}

      {data.next_action && (
        <section>
          <SectionTitle>Next action</SectionTitle>
          <p className="text-sm text-text-secondary">{data.next_action}</p>
        </section>
      )}

      {data.status === "awaiting_approval" && (
        <ApprovalCard slug={slug} data={data} />
      )}

      {data.build_verification && (
        <section>
          <SectionTitle>Build verification</SectionTitle>
          <BuildVerificationCard v={data.build_verification} />
        </section>
      )}

      {data.quality_summary && (
        <section>
          <SectionTitle>Quality</SectionTitle>
          <QualityCard q={data.quality_summary} />
        </section>
      )}

      {Array.isArray(data.stages) && data.stages.length > 0 && (
        <section>
          <SectionTitle>Stages</SectionTitle>
          <ol className="space-y-2">
            {data.stages.map((s: any, i: number) => (
              <StageRow key={i} stage={s} index={i} />
            ))}
          </ol>
        </section>
      )}

      {Array.isArray(data.artifacts) && data.artifacts.length > 0 && (
        <section>
          <SectionTitle>Artifacts ({data.artifacts.length})</SectionTitle>
          <ul className="text-sm font-mono text-text-secondary bg-bg-2 border border-border rounded-lg p-3 space-y-0.5 max-h-64 overflow-y-auto">
            {data.artifacts.map((path: string) => (
              <li key={path} className="truncate" title={path}>
                {path}
              </li>
            ))}
          </ul>
        </section>
      )}

      {Array.isArray(data.history) && data.history.length > 0 && (
        <section>
          <SectionTitle>History</SectionTitle>
          <ol className="text-xs space-y-1 max-h-64 overflow-y-auto bg-bg-2 border border-border rounded-lg p-3">
            {data.history.map((h: any, i: number) => (
              <li key={i} className="flex gap-3">
                <span className="text-text-dim font-mono shrink-0">
                  {new Date(h.ts * 1000).toLocaleTimeString()}
                </span>
                <span className="text-accent font-mono shrink-0">{h.event}</span>
                {h.message && (
                  <span className="text-text-secondary truncate" title={h.message}>
                    {h.message}
                  </span>
                )}
              </li>
            ))}
          </ol>
        </section>
      )}
    </div>
  );
}

function StageRow({ stage, index }: { stage: any; index: number }) {
  const status = stage.status ?? "pending";
  return (
    <li className="rounded-lg border border-border bg-bg-2 p-3">
      <div className="flex items-baseline justify-between gap-3 min-w-0">
        <div className="min-w-0">
          <span className="text-xs font-mono text-text-dim mr-2">
            #{index + 1}
          </span>
          <span className="font-medium">{stage.name}</span>
          {stage.agent && (
            <span className="text-xs text-text-secondary ml-2">
              · {stage.agent}
            </span>
          )}
        </div>
        <StatusPill status={status} />
      </div>
      {stage.expected_artifact && (
        <div className="text-xs text-text-dim font-mono mt-1 truncate">
          → {stage.expected_artifact}
        </div>
      )}
      {stage.summary && (
        <p className="text-xs text-text-secondary mt-1 whitespace-pre-wrap break-words">
          {stage.summary}
        </p>
      )}
      {stage.error && (
        <p className="text-xs text-status-red mt-1 whitespace-pre-wrap break-words">
          {stage.error}
        </p>
      )}
      {Array.isArray(stage.files) && stage.files.length > 0 && (
        <ul className="text-[0.65rem] font-mono text-text-secondary mt-1.5 space-y-0.5">
          {stage.files.slice(0, 8).map((f: string) => (
            <li key={f} className="truncate" title={f}>
              · {f}
            </li>
          ))}
          {stage.files.length > 8 && (
            <li className="text-text-dim">+ {stage.files.length - 8} more</li>
          )}
        </ul>
      )}
    </li>
  );
}

function BuildVerificationCard({ v }: { v: any }) {
  const verdict = (v.verdict ?? "unknown").toString();
  const color =
    verdict === "passed"
      ? "border-status-green/40 bg-status-green/10 text-status-green"
      : verdict === "failed"
        ? "border-status-red/40 bg-status-red/10 text-status-red"
        : "border-border bg-bg-2 text-text-secondary";
  return (
    <div className={`rounded-lg border p-3 text-sm ${color}`}>
      <div className="flex items-baseline justify-between gap-3">
        <span className="font-mono uppercase tracking-wider text-xs">
          {verdict}
        </span>
        {v.stack && (
          <span className="text-xs text-text-dim font-mono">stack: {v.stack}</span>
        )}
      </div>
      {v.command && (
        <pre className="text-xs font-mono mt-2 whitespace-pre-wrap break-all">
          $ {v.command}
        </pre>
      )}
      {v.summary && (
        <p className="text-xs mt-2 whitespace-pre-wrap break-words">{v.summary}</p>
      )}
    </div>
  );
}

function QualityCard({ q }: { q: any }) {
  return (
    <div className="rounded-lg border border-border bg-bg-2 p-3 text-sm">
      <div className="flex items-baseline gap-3">
        <span className="font-mono text-accent">{q.verdict ?? "—"}</span>
        {typeof q.score === "number" && (
          <span className="text-xs text-text-dim font-mono">
            score {Math.round(q.score * 100)}%
          </span>
        )}
      </div>
      {q.summary && (
        <p className="text-xs text-text-secondary mt-2 whitespace-pre-wrap break-words">
          {q.summary}
        </p>
      )}
    </div>
  );
}

function NewProjectForm({
  onStarted,
}: {
  onStarted: (slug?: string) => void;
}) {
  const templates = useQuery({
    queryKey: ["studio_templates"],
    queryFn: api.templates,
  });
  const [template, setTemplate] = useState<string>("");
  const [brief, setBrief] = useState("");
  const [slug, setSlug] = useState("");
  const [missionGoal, setMissionGoal] = useState("");
  const [repoTarget, setRepoTarget] = useState("");

  const start = useMutation({
    mutationFn: () => {
      const payload: {
        template: string;
        brief?: string;
        slug?: string;
        mission_setup?: Record<string, unknown>;
        repo_target?: Record<string, unknown>;
      } = { template };
      if (brief.trim()) payload.brief = brief.trim();
      if (slug.trim()) payload.slug = slug.trim();
      if (missionGoal.trim()) payload.mission_setup = { goal: missionGoal.trim() };
      if (repoTarget.trim()) payload.repo_target = { remote: repoTarget.trim() };
      return api.startStudio(payload);
    },
    onSuccess: (res) => {
      if (res.accepted) {
        onStarted(res.slug);
        setBrief("");
        setSlug("");
        setMissionGoal("");
        setRepoTarget("");
      }
    },
  });

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!template) return;
    start.mutate();
  }

  const list: Template[] = templates.data?.templates ?? [];
  const selectedTemplate = list.find((t) => t.key === template);

  return (
    <form onSubmit={onSubmit} className="space-y-4 max-w-2xl">
      <SectionTitle>Brief the swarm</SectionTitle>
      <div>
        <label className="text-xs uppercase tracking-wider text-text-secondary block mb-1">
          Template
        </label>
        <select
          value={template}
          onChange={(e) => setTemplate(e.target.value)}
          className="w-full bg-bg-2 border border-border rounded px-3 py-2 text-sm outline-none focus:border-accent"
          required
        >
          <option value="">Pick a template…</option>
          {list.map((t) => (
            <option key={t.key} value={t.key}>
              {t.title}
            </option>
          ))}
        </select>
        {selectedTemplate?.description && (
          <p className="text-xs text-text-secondary mt-1">
            {selectedTemplate.description}
          </p>
        )}
      </div>

      <div>
        <label className="text-xs uppercase tracking-wider text-text-secondary block mb-1">
          Brief
        </label>
        <textarea
          value={brief}
          onChange={(e) => setBrief(e.target.value)}
          placeholder="A real, full-blown program — describe what you want built."
          rows={5}
          className="w-full bg-bg-2 border border-border rounded px-3 py-2 text-sm outline-none focus:border-accent resize-y"
        />
      </div>

      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className="text-xs uppercase tracking-wider text-text-secondary block mb-1">
            Slug (optional)
          </label>
          <input
            value={slug}
            onChange={(e) => setSlug(e.target.value)}
            placeholder="my-app"
            className="w-full bg-bg-2 border border-border rounded px-3 py-2 text-sm font-mono outline-none focus:border-accent"
          />
        </div>
        <div>
          <label className="text-xs uppercase tracking-wider text-text-secondary block mb-1">
            Repo target (optional)
          </label>
          <input
            value={repoTarget}
            onChange={(e) => setRepoTarget(e.target.value)}
            placeholder="git@github.com:you/repo.git"
            className="w-full bg-bg-2 border border-border rounded px-3 py-2 text-sm font-mono outline-none focus:border-accent"
          />
        </div>
      </div>

      <div>
        <label className="text-xs uppercase tracking-wider text-text-secondary block mb-1">
          Mission goal (optional)
        </label>
        <input
          value={missionGoal}
          onChange={(e) => setMissionGoal(e.target.value)}
          placeholder="What does success look like?"
          className="w-full bg-bg-2 border border-border rounded px-3 py-2 text-sm outline-none focus:border-accent"
        />
      </div>

      {selectedTemplate?.stages && selectedTemplate.stages.length > 0 && (
        <div className="rounded-lg border border-border bg-bg-2 p-3">
          <div className="text-xs uppercase tracking-wider text-text-secondary mb-2">
            Stages
          </div>
          <ol className="text-xs space-y-0.5">
            {selectedTemplate.stages.map((s, i) => (
              <li key={i} className="font-mono text-text-secondary">
                <span className="text-text-dim mr-2">#{i + 1}</span>
                {s.name}
                <span className="text-text-dim"> · {s.agent}</span>
              </li>
            ))}
          </ol>
        </div>
      )}

      <div className="flex items-center gap-3">
        <button
          type="submit"
          disabled={!template || start.isPending}
          className="rounded bg-accent text-bg-0 font-medium px-4 py-2 text-sm disabled:opacity-60"
        >
          {start.isPending ? "Starting…" : "Start build"}
        </button>
        {start.error && (
          <span className="text-status-red text-xs">
            {start.error instanceof Error ? start.error.message : "failed"}
          </span>
        )}
        {start.data?.error && (
          <span className="text-status-red text-xs">{start.data.error}</span>
        )}
      </div>
    </form>
  );
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <h3 className="text-xs uppercase tracking-wider text-text-secondary font-medium mb-2">
      {children}
    </h3>
  );
}

function StatusPill({ status }: { status: string }) {
  const s = (status ?? "unknown").toString();
  const color =
    s === "completed" || s === "passed" || s === "done"
      ? "bg-status-green/20 text-status-green border-status-green/30"
      : s === "running" || s === "in_progress" || s === "active" || s === "busy"
        ? "bg-accent-soft text-accent border-accent-line"
        : s === "failed" || s === "error"
          ? "bg-status-red/20 text-status-red border-status-red/30"
          : s === "blocked" ||
              s === "needs_input" ||
              s === "awaiting_approval" ||
              s === "awaiting_clarification"
            ? "bg-status-yellow/20 text-status-yellow border-status-yellow/30"
            : "bg-bg-3 text-text-dim border-border";
  return (
    <span
      className={`inline-block px-2 py-0.5 rounded text-[0.65rem] uppercase tracking-wider border shrink-0 ${color}`}
    >
      {s}
    </span>
  );
}

// Approval gate UI. Rendered when manifest.status === "awaiting_approval"
// — fetches architecture.md, lets the user edit in place, and exposes
// approve / approve-with-edits / reject actions. Mirrors the dashboard's
// 4s React Query poll so we don't need WebSocket plumbing.
function ApprovalCard({ slug, data }: { slug: string; data: any }) {
  const qc = useQueryClient();
  const arch = useQuery({
    queryKey: ["architecture_md", slug, data?.updated_at],
    queryFn: () => api.fetchArchitecture(slug),
    retry: false,
  });
  const [content, setContent] = useState<string>("");
  const [feedback, setFeedback] = useState<string>("");
  const [rejectMode, setRejectMode] = useState<boolean>(false);

  useEffect(() => {
    if (typeof arch.data === "string") {
      setContent(arch.data);
    }
  }, [arch.data]);

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["studio_project", slug] });
    qc.invalidateQueries({ queryKey: ["studio_projects"] });
  };

  const approve = useMutation({
    mutationFn: () => api.approveProject(slug),
    onSuccess: invalidate,
  });
  const approveEdits = useMutation({
    mutationFn: () => api.approveProjectWithEdits(slug, content),
    onSuccess: invalidate,
  });
  const reject = useMutation({
    mutationFn: () => api.rejectProject(slug, feedback),
    onSuccess: () => {
      invalidate();
      setRejectMode(false);
      setFeedback("");
    },
  });

  const original = typeof arch.data === "string" ? arch.data : "";
  const edited = content.trim() !== original.trim();
  const busy =
    approve.isPending || approveEdits.isPending || reject.isPending;

  const gateInfo = data?.awaiting_approval_for ?? {};
  const stageName = gateInfo.stage || "stage";
  const agentName = gateInfo.agent || "agent";

  return (
    <section className="border border-status-yellow/40 bg-status-yellow/5 rounded-lg p-4 space-y-3">
      <div className="flex items-baseline justify-between">
        <SectionTitle>
          Approval required · {stageName} ({agentName})
        </SectionTitle>
        <span className="text-[0.65rem] uppercase tracking-wider text-status-yellow">
          Pipeline halted
        </span>
      </div>
      <p className="text-xs text-text-secondary">
        Review the architecture below. Edit inline to apply changes, or
        reject to send feedback back to the architect and re-run the stage.
      </p>
      {arch.isLoading && (
        <p className="text-xs text-text-dim">Loading architecture.md…</p>
      )}
      {arch.error && (
        <p className="text-xs text-status-red">
          Could not load architecture.md:{" "}
          {arch.error instanceof Error ? arch.error.message : "error"}
        </p>
      )}
      {typeof arch.data === "string" && (
        <textarea
          value={content}
          onChange={(e) => setContent(e.target.value)}
          rows={20}
          className="w-full font-mono text-xs bg-bg-2 border border-border rounded p-2 text-text-primary"
          spellCheck={false}
        />
      )}
      <div className="flex flex-wrap items-center gap-2">
        <button
          onClick={() => approve.mutate()}
          disabled={busy || edited}
          title={
            edited
              ? "You have edits — use 'Approve with edits'"
              : "Approve unchanged and continue to the next stage"
          }
          className="rounded bg-status-green/30 border border-status-green/40 text-status-green text-xs px-3 py-1.5 disabled:opacity-50"
        >
          {approve.isPending ? "Approving…" : "Approve"}
        </button>
        <button
          onClick={() => approveEdits.mutate()}
          disabled={busy || !edited}
          className="rounded bg-accent-soft border border-accent-line text-accent text-xs px-3 py-1.5 disabled:opacity-50"
        >
          {approveEdits.isPending ? "Saving…" : "Approve with edits"}
        </button>
        <button
          onClick={() => setRejectMode((v) => !v)}
          disabled={busy}
          className="rounded border border-status-red/40 text-status-red text-xs px-3 py-1.5 disabled:opacity-50"
        >
          Reject
        </button>
      </div>
      {rejectMode && (
        <div className="space-y-2 border-t border-border pt-3">
          <label className="text-xs text-text-secondary">
            Feedback for the architect (prepended to the brief on re-run):
          </label>
          <textarea
            value={feedback}
            onChange={(e) => setFeedback(e.target.value)}
            rows={4}
            className="w-full text-xs bg-bg-2 border border-border rounded p-2 text-text-primary"
          />
          <div className="flex gap-2">
            <button
              onClick={() => reject.mutate()}
              disabled={busy || !feedback.trim()}
              className="rounded bg-status-red/20 border border-status-red/40 text-status-red text-xs px-3 py-1.5 disabled:opacity-50"
            >
              {reject.isPending ? "Rejecting…" : "Send feedback & re-run"}
            </button>
            <button
              onClick={() => setRejectMode(false)}
              disabled={busy}
              className="rounded border border-border text-text-secondary text-xs px-3 py-1.5"
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </section>
  );
}

// Compact token formatter. 1234 → "1.2K tok", 1234567 → "1.2M tok".
// Estimated 4-chars-per-token from LLM_EXCHANGE event payloads.
function fmtTokensCompact(n: number): string {
  if (!n || n < 0) return "0 tok";
  if (n < 1000) return `${n} tok`;
  if (n < 1_000_000) return `${(n / 1000).toFixed(n < 10000 ? 1 : 0)}K tok`;
  return `${(n / 1_000_000).toFixed(n < 10_000_000 ? 2 : 1)}M tok`;
}
