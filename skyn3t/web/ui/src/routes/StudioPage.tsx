import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ProjectDetail, ProjectRow, Template } from "../api/client";
import { BuildConsole } from "../components/studio/BuildConsole";
import {
  buildClarificationAnswers,
  clarificationOptionEntry,
  clarificationSubmitReady,
  missingClarificationQuestions,
} from "./clarificationForm";

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
      {error != null && (
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
          const artifactCount = artifactPaths(p.artifacts).length;
          const designCount = designArtifactPaths(artifactPaths(p.artifacts)).length;
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
                <div className="mt-2 flex flex-wrap items-center gap-1.5 text-[0.65rem]">
                  <span className="rounded-full border border-border bg-bg-3 px-2 py-0.5 text-text-dim">
                    {artifactCount} artifact{artifactCount === 1 ? "" : "s"}
                  </span>
                  {designCount > 0 && (
                    <span className="rounded-full border border-accent-line bg-accent-soft px-2 py-0.5 text-accent">
                      Design export ready · {designCount}
                    </span>
                  )}
                </div>
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

  const artifacts = artifactPaths(data.artifacts);
  const designArtifacts = designArtifactPaths(artifacts);
  const preferredArtifact = preferredArtifactPath(artifacts);
  const stageSummary = summarizeStages(data.stages);
  const quality = data.quality_summary;
  const verification = data.build_verification;
  const penpotReady = designArtifacts.length > 0;

  return (
    <div className="space-y-6 min-w-0">
      <section className="rounded-2xl border border-accent-line/60 bg-[radial-gradient(circle_at_top_right,rgba(15,240,252,0.12),transparent_32%),linear-gradient(180deg,rgba(18,25,35,0.96),rgba(6,10,16,0.98))] p-5 shadow-[0_18px_48px_rgba(0,0,0,0.28)]">
        <div className="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <StatusPill status={data.status} />
              <span className="rounded-full border border-border bg-bg-3 px-2 py-0.5 text-[0.65rem] uppercase tracking-wider text-text-dim">
                {data.template ?? "template unknown"}
              </span>
              <span className="rounded-full border border-border bg-bg-3 px-2 py-0.5 text-[0.65rem] uppercase tracking-wider text-text-dim">
                {stageSummary.done}/{stageSummary.total || 0} stages
              </span>
              <span className="rounded-full border border-border bg-bg-3 px-2 py-0.5 text-[0.65rem] uppercase tracking-wider text-text-dim">
                {artifacts.length} artifact{artifacts.length === 1 ? "" : "s"}
              </span>
              {penpotReady && (
                <span className="rounded-full border border-accent-line bg-accent-soft px-2 py-0.5 text-[0.65rem] uppercase tracking-wider text-accent">
                  Design export ready · {designArtifacts.length}
                </span>
              )}
            </div>
            <h2
              className="mt-3 text-3xl font-semibold tracking-tight text-text-primary"
              title={data.title || slug}
            >
              {data.title || slug}
            </h2>
            <div className="text-xs text-text-dim font-mono mt-1 truncate">
              {slug}
            </div>
            <p className="mt-3 max-w-3xl text-sm leading-6 text-text-secondary">
              {data.next_action ||
                "Studio is holding the latest project state here so you can inspect, package, and continue the mission."}
            </p>
          </div>
          <div className="flex shrink-0 flex-wrap items-center gap-2">
            {preferredArtifact && (
              <a
                href={artifactOpenUrl(slug, preferredArtifact)}
                target="_blank"
                rel="noreferrer"
                className="rounded bg-accent px-3 py-2 text-xs font-medium text-bg-0"
              >
                <i className="fa-solid fa-play mr-1.5" />
                {primaryArtifactLabel(preferredArtifact)}
              </a>
            )}
            <a
              href={projectZipUrl(slug)}
              className="rounded border border-border bg-bg-3 px-3 py-2 text-xs font-medium text-text-primary hover:border-border-strong"
            >
              <i className="fa-solid fa-download mr-1.5" />
              Download zip
            </a>
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
              className="rounded border border-status-red/40 px-3 py-2 text-xs font-medium text-status-red hover:bg-status-red/10 disabled:opacity-60"
            >
              <i className="fa-solid fa-trash mr-1.5" />
              Delete
            </button>
            {penpotReady && (
              <span className="rounded-full border border-accent-line bg-accent-soft px-2 py-1 text-[0.65rem] uppercase tracking-wider text-accent">
                Optional design export
              </span>
            )}
          </div>
        </div>

        <div className="mt-5 grid gap-4 xl:grid-cols-[minmax(0,1.4fr)_minmax(320px,0.9fr)]">
          <div className="rounded-xl border border-border bg-bg-2/80 p-4">
            <SectionTitle>Mission pulse</SectionTitle>
            <div className="space-y-3">
              <div>
                <div className="text-[0.7rem] uppercase tracking-wider text-text-dim">
                  Current focus
                </div>
                <p className="mt-1 text-sm leading-6 text-text-primary">
                  {data.next_action || "Waiting for the next mission update."}
                </p>
              </div>
              {data.brief && (
                <div>
                  <div className="text-[0.7rem] uppercase tracking-wider text-text-dim">
                    Mission brief
                  </div>
                  <p className="mt-1 text-sm leading-6 text-text-secondary whitespace-pre-wrap break-words">
                    {data.brief}
                  </p>
                </div>
              )}
            </div>
          </div>

          <div className="rounded-xl border border-border bg-bg-2/80 p-4">
            <SectionTitle>Open result</SectionTitle>
            <div className="space-y-3 text-sm text-text-secondary">
              <p>
                Jump straight into the main output for this build, or download
                the full project bundle if you want to inspect everything.
              </p>
              <div className="flex flex-wrap gap-2">
                {preferredArtifact ? (
                  <a
                    href={artifactOpenUrl(slug, preferredArtifact)}
                    target="_blank"
                    rel="noreferrer"
                    className="rounded border border-border bg-bg-3 px-3 py-2 text-xs font-medium text-text-primary hover:border-border-strong"
                  >
                    <i className="fa-solid fa-eye mr-1.5" />
                    {primaryArtifactLabel(preferredArtifact)}
                  </a>
                ) : null}
                <a
                  href={projectZipUrl(slug)}
                  className="rounded border border-border bg-bg-3 px-3 py-2 text-xs font-medium text-text-primary hover:border-border-strong"
                >
                  <i className="fa-solid fa-file-zipper mr-1.5" />
                  Download project zip
                </a>
              </div>
            </div>
          </div>
        </div>
      </section>

      <section>
        <SectionTitle>Live build console</SectionTitle>
        <BuildConsole slug={slug} sessionId={data.session_id} />
      </section>

      {data.status === "awaiting_clarification" && (
        <ClarificationCard slug={slug} data={data} />
      )}

      {data.status === "awaiting_approval" && (
        <ApprovalCard slug={slug} data={data} />
      )}

      <div className="grid gap-5 xl:grid-cols-2">
        {penpotReady && (
          <section>
            <SectionTitle>Design exports (optional)</SectionTitle>
            <DesignHandoffCard slug={slug} artifacts={artifacts} />
          </section>
        )}

        {verification && (
          <section>
            <SectionTitle>Build verification</SectionTitle>
            <BuildVerificationCard v={verification} />
          </section>
        )}

        {quality && (
          <section>
            <SectionTitle>Quality</SectionTitle>
            <QualityCard q={quality} />
          </section>
        )}

        <section>
          <SectionTitle>Artifacts ({artifacts.length})</SectionTitle>
          <ArtifactList slug={slug} artifacts={artifacts} />
        </section>
      </div>

      {Array.isArray(data.stages) && data.stages.length > 0 && (
        <section>
          <SectionTitle>Stages</SectionTitle>
          <ol className="grid gap-3 xl:grid-cols-2">
            {data.stages.map((s: any, i: number) => (
              <StageRow key={i} stage={s} index={i} />
            ))}
          </ol>
        </section>
      )}

      {Array.isArray(data.history) && data.history.length > 0 && (
        <section>
          <SectionTitle>History</SectionTitle>
          <ol className="text-xs space-y-2 max-h-72 overflow-y-auto rounded-xl border border-border bg-bg-2 p-3">
            {data.history.map((h: any, i: number) => (
              <li
                key={i}
                className="rounded-lg border border-border bg-bg-3/70 px-3 py-2"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-text-dim font-mono shrink-0">
                    {new Date(h.ts * 1000).toLocaleTimeString()}
                  </span>
                  <span className="text-accent font-mono shrink-0">{h.event}</span>
                  {h.status && <StatusPill status={h.status} />}
                </div>
                {h.message && (
                  <p
                    className="mt-1.5 text-text-secondary whitespace-pre-wrap break-words"
                    title={h.message}
                  >
                    {h.message}
                  </p>
                )}
              </li>
            ))}
          </ol>
        </section>
      )}
    </div>
  );
}

function DesignHandoffCard({
  slug,
  artifacts,
}: {
  slug: string;
  artifacts: string[];
}) {
  const designArtifacts = designArtifactPaths(artifacts);

  return (
    <div className="rounded-xl border border-border bg-bg-2 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <div className="text-sm font-medium text-text-primary">
            Optional Penpot export
          </div>
          <p className="mt-1 text-xs leading-5 text-text-secondary">
            Use this only if you want to take the generated design assets into
            Download design export package for another design pass.
          </p>
        </div>
        {designArtifacts.length > 0 ? (
          <span className="rounded-full border border-accent-line bg-accent-soft px-2 py-0.5 text-[0.65rem] uppercase tracking-wider text-accent">
            Optional · {designArtifacts.length}
          </span>
        ) : (
          <span className="rounded-full border border-border bg-bg-3 px-2 py-0.5 text-[0.65rem] uppercase tracking-wider text-text-dim">
            Waiting on design assets
          </span>
        )}
      </div>

      {designArtifacts.length > 0 ? (
        <>
          <div className="mt-4 flex flex-wrap gap-2">
            <a
              href={projectPenpotPackageUrl(slug)}
              className="rounded bg-accent px-3 py-2 text-xs font-medium text-bg-0"
            >
              <i className="fa-solid fa-compass-drafting mr-1.5" />
              Download handoff
            </a>
            <a
              href={projectPenpotManifestUrl(slug)}
              target="_blank"
              rel="noreferrer"
              className="rounded border border-accent-line bg-accent-soft px-3 py-2 text-xs font-medium text-accent"
            >
              <i className="fa-solid fa-file-code mr-1.5" />
              Open manifest
            </a>
          </div>
          <div className="mt-4 flex flex-wrap gap-2">
            {designArtifacts.map((path) => (
              <a
                key={path}
                href={artifactOpenUrl(slug, path)}
                target="_blank"
                rel="noreferrer"
                className="rounded-full border border-border bg-bg-3 px-2.5 py-1 text-[0.7rem] font-mono text-text-secondary hover:border-border-strong hover:text-text-primary"
                title={path}
              >
                {path}
              </a>
            ))}
          </div>
        </>
      ) : (
        <p className="mt-4 rounded-lg border border-border bg-bg-3 px-3 py-2 text-xs leading-5 text-text-dim">
          As soon as this mission writes files like <code>tokens.json</code>,{" "}
          <code>palette.json</code>, <code>brand.md</code>,{" "}
          <code>components.md</code>, or <code>logo.svg</code>, the Penpot
          export buttons will appear here.
        </p>
      )}
    </div>
  );
}

function ArtifactList({ slug, artifacts }: { slug: string; artifacts: string[] }) {
  if (!artifacts.length) {
    return (
      <div className="rounded-xl border border-border bg-bg-2 p-4 text-sm text-text-dim">
        No artifacts yet.
      </div>
    );
  }

  return (
    <ul className="max-h-80 space-y-2 overflow-y-auto rounded-xl border border-border bg-bg-2 p-3">
      {artifacts.map((path) => (
        <li
          key={path}
          className="flex items-center justify-between gap-3 rounded-lg border border-border bg-bg-3/70 px-3 py-2"
        >
          <div className="min-w-0">
            <div className="truncate font-mono text-xs text-text-primary" title={path}>
              {path}
            </div>
            <div className="mt-0.5 text-[0.65rem] uppercase tracking-wider text-text-dim">
              {artifactPreviewMode(path) === "preview" ? "previewable output" : "saved artifact"}
            </div>
          </div>
          <a
            href={artifactOpenUrl(slug, path)}
            target="_blank"
            rel="noreferrer"
            className="shrink-0 rounded border border-border px-2.5 py-1 text-[0.7rem] font-medium text-text-secondary hover:border-border-strong hover:text-text-primary"
          >
            Open
          </a>
        </li>
      ))}
    </ul>
  );
}

function artifactPaths(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => (typeof item === "string" ? item : ""))
    .filter(Boolean);
}

function designArtifactPaths(artifacts: string[]): string[] {
  const patterns = [
    /(^|\/)tokens\.(json|css)$/i,
    /(^|\/)palette\.json$/i,
    /(^|\/)brand\.md$/i,
    /(^|\/)components\.md$/i,
    /(^|\/)logo\.svg$/i,
    /(^|\/)readme\.md$/i,
  ];
  const seen = new Set<string>();
  const matches: string[] = [];

  for (const pattern of patterns) {
    for (const path of artifacts) {
      if (pattern.test(path) && !seen.has(path)) {
        seen.add(path);
        matches.push(path);
      }
    }
  }

  for (const path of artifacts) {
    const name = path.split("/").pop() || path;
    if (
      !seen.has(path) &&
      /(design|token|palette|brand|logo|component)/i.test(name)
    ) {
      seen.add(path);
      matches.push(path);
    }
  }

  return matches.slice(0, 8);
}

function preferredArtifactPath(artifacts: string[]): string {
  return (
    artifacts.find((path) => /(^|\/)(readme\.md|spec\.md|plan\.md|index\.html)$/i.test(path)) ??
    artifacts.find((path) => !/review\.md$/i.test(path)) ??
    artifacts[0] ??
    ""
  );
}

function primaryArtifactLabel(path: string): string {
  return artifactPreviewMode(path) === "preview"
    ? "Open app preview"
    : "Open key artifact";
}

function artifactPreviewMode(path: string): "preview" | "file" {
  const normalized = String(path || "").trim().toLowerCase().split("?")[0];
  if (/\.(html?|svg|pdf|png|jpe?g|gif|webp|avif)$/i.test(normalized)) {
    return "preview";
  }
  return "file";
}

function artifactOpenUrl(slug: string, path: string): string {
  return artifactPreviewMode(path) === "preview"
    ? projectPreviewUrl(slug, path)
    : projectFileUrl(slug, path);
}

function projectZipUrl(slug: string): string {
  return `/api/studio/projects/${encodeURIComponent(slug)}/zip`;
}

function projectPenpotPackageUrl(slug: string): string {
  return `/api/studio/projects/${encodeURIComponent(slug)}/design-handoff/penpot/package`;
}

function projectPenpotManifestUrl(slug: string): string {
  return `/api/studio/projects/${encodeURIComponent(slug)}/design-handoff/penpot`;
}

function projectFileUrl(slug: string, path: string): string {
  return `/api/studio/projects/${encodeURIComponent(slug)}/file?path=${encodeURIComponent(path)}`;
}

function projectPreviewUrl(slug: string, path: string): string {
  const safePath = String(path || "")
    .split("/")
    .filter((part) => part && part !== ".")
    .map((part) => encodeURIComponent(part))
    .join("/");
  return `/api/studio/projects/${encodeURIComponent(slug)}/preview/${safePath}`;
}

function summarizeStages(stages: ProjectDetail["stages"] | undefined): {
  total: number;
  done: number;
} {
  const stageList = Array.isArray(stages) ? stages : [];
  return {
    total: stageList.length,
    done: stageList.filter((stage) =>
      ["done", "completed", "passed"].includes(
        String(stage.status ?? "").toLowerCase(),
      ),
    ).length,
  };
}

function StageRow({ stage, index }: { stage: any; index: number }) {
  const status = stage.status ?? "pending";
  return (
    <li className="rounded-xl border border-border bg-[linear-gradient(180deg,rgba(22,31,43,0.92),rgba(10,15,22,0.96))] p-3 shadow-[0_10px_28px_rgba(0,0,0,0.16)]">
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
  const [template, setTemplate] = useState<string>("auto");
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [brief, setBrief] = useState("");
  const [slug, setSlug] = useState("");
  const [missionGoal, setMissionGoal] = useState("");
  const [missionAudience, setMissionAudience] = useState("");
  const [missionAutonomy, setMissionAutonomy] = useState("confirm_first");
  const [repoTarget, setRepoTarget] = useState("");

  const start = useMutation({
    mutationFn: () => {
      const payload: {
        template: string;
        brief?: string;
        slug?: string;
        mission_setup?: Record<string, unknown>;
        repo_target?: Record<string, unknown>;
      } = { template: effectiveTemplate };
      if (brief.trim()) payload.brief = brief.trim();
      if (slug.trim()) payload.slug = slug.trim();
      if (missionGoal.trim() || missionAudience || missionAutonomy) {
        payload.mission_setup = {
          ...(missionGoal.trim() ? { goal: missionGoal.trim() } : {}),
          ...(missionAudience ? { audience: missionAudience } : {}),
          ...(missionAutonomy ? { autonomy: missionAutonomy } : {}),
        };
      }
      if (repoTarget.trim()) payload.repo_target = { remote: repoTarget.trim() };
      return api.startStudio(payload);
    },
    onSuccess: (res) => {
      if (res.accepted) {
        onStarted(res.slug);
        setBrief("");
        setSlug("");
        setMissionGoal("");
        setMissionAudience("");
        setMissionAutonomy("confirm_first");
        setRepoTarget("");
      }
    },
  });

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    start.mutate();
  }

  const list: Template[] = templates.data?.templates ?? [];
  const selectedTemplate = list.find((t) => t.key === template);
  const effectiveTemplate = template || "auto";

  return (
    <form onSubmit={onSubmit} className="space-y-4 max-w-2xl">
      <SectionTitle>Brief the swarm</SectionTitle>
      <p className="text-xs text-text-secondary">
        Describe what you want in plain language. SkyN3t uses the{" "}
        <span className="font-mono text-accent">auto</span> pipeline and asks
        simple follow-ups when needed.
      </p>

      {showAdvanced && (
        <div>
          <label className="text-xs uppercase tracking-wider text-text-secondary block mb-1">
            Template (advanced)
          </label>
          <select
            value={effectiveTemplate}
            onChange={(e) => setTemplate(e.target.value)}
            className="w-full bg-bg-2 border border-border rounded px-3 py-2 text-sm outline-none focus:border-accent"
          >
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
      )}

      {!showAdvanced && (
        <button
          type="button"
          onClick={() => setShowAdvanced(true)}
          className="text-xs text-text-dim underline underline-offset-2 hover:text-text-secondary"
        >
          Advanced: pick a fixed template
        </button>
      )}

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

      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className="text-xs uppercase tracking-wider text-text-secondary block mb-1">
            Audience (optional)
          </label>
          <select
            value={missionAudience}
            onChange={(e) => setMissionAudience(e.target.value)}
            className="w-full bg-bg-2 border border-border rounded px-3 py-2 text-sm outline-none focus:border-accent"
          >
            <option value="">Default</option>
            <option value="general">General users</option>
            <option value="builders">Builders / developers</option>
            <option value="team">Internal team</option>
            <option value="leaders">Decision-makers</option>
            <option value="investors">Investors / partners</option>
          </select>
        </div>
        <div>
          <label className="text-xs uppercase tracking-wider text-text-secondary block mb-1">
            Build mode
          </label>
          <select
            value={missionAutonomy}
            onChange={(e) => setMissionAutonomy(e.target.value)}
            className="w-full bg-bg-2 border border-border rounded px-3 py-2 text-sm outline-none focus:border-accent"
          >
            <option value="confirm_first">Confirm first (ask plain questions)</option>
            <option value="balanced">Balanced</option>
            <option value="move_fast">Move fast</option>
          </select>
        </div>
      </div>

      <div>
        <label className="text-xs uppercase tracking-wider text-text-secondary block mb-1">
          Success goal (optional)
        </label>
        <input
          value={missionGoal}
          onChange={(e) => setMissionGoal(e.target.value)}
          placeholder="What does success look like?"
          className="w-full bg-bg-2 border border-border rounded px-3 py-2 text-sm outline-none focus:border-accent"
        />
      </div>

      {showAdvanced && selectedTemplate?.stages && selectedTemplate.stages.length > 0 && (
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
          disabled={start.isPending}
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

function ClarificationCard({ slug, data }: { slug: string; data: any }) {
  const qc = useQueryClient();
  const questions = Array.isArray(data?.clarification?.questions)
    ? data.clarification.questions
        .map((q: unknown) => String(q ?? "").trim())
        .filter(Boolean)
    : [];
  const questionOptions = Array.isArray(data?.clarification?.question_options)
    ? data.clarification.question_options
    : [];
  const [answers, setAnswers] = useState<string[]>([]);
  const [sent, setSent] = useState(false);
  const briefFallback = String(data?.brief_raw || data?.brief || "").trim();

  useEffect(() => {
    setAnswers((prev) => {
      if (prev.length === questions.length) {
        return prev;
      }
      return questions.map((_: unknown, index: number) => prev[index] ?? "");
    });
    setSent(false);
  }, [questions.join("\n"), questions.length]);

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["studio_project", slug] });
    qc.invalidateQueries({ queryKey: ["studio_projects"] });
  };

  const payloadAnswers = buildClarificationAnswers(
    questions,
    answers,
    questionOptions,
    briefFallback,
  );

  const submit = useMutation({
    mutationFn: () => api.clarifyProject(slug, payloadAnswers),
    onSuccess: () => {
      setSent(true);
      invalidate();
    },
  });

  const busy = submit.isPending || sent;
  const askedBy = String(data?.clarification?.asked_by || "agent");
  const ready = clarificationSubmitReady(questions, answers, questionOptions);
  const missing = missingClarificationQuestions(questions, answers, questionOptions);

  function setAnswer(index: number, value: string) {
    setAnswers((prev) => {
      const next = [...prev];
      while (next.length < questions.length) {
        next.push("");
      }
      next[index] = value;
      return next;
    });
  }

  return (
    <section className="border border-status-yellow/40 bg-status-yellow/5 rounded-lg p-4 space-y-4">
      <div className="flex items-baseline justify-between gap-3">
        <SectionTitle>Quick questions · {askedBy}</SectionTitle>
        <span className="text-[0.65rem] uppercase tracking-wider text-status-yellow">
          {sent ? "Resuming build" : "Waiting on answers"}
        </span>
      </div>
      <p className="text-xs text-text-secondary">
        Pick an option or type a short answer. Studio will resume the build
        with your choices — no technical jargon required.
      </p>
      <div className="space-y-3">
        {questions.map((question: string, index: number) => {
          const optionEntry = clarificationOptionEntry(
            question,
            index,
            questionOptions,
          );
          const options = Array.isArray(optionEntry?.options)
            ? optionEntry.options
            : [];
          const placeholder =
            String(optionEntry?.placeholder || "").trim() ||
            (options.length > 0
              ? "Or type a custom answer…"
              : "Optional — leave blank to use your original brief.");
          const missingRequired =
            missing.includes(question) && !String(answers[index] ?? "").trim();
          return (
            <div
              key={`${index}-${question}`}
              className={`block rounded-lg border bg-bg-2/80 p-3 space-y-2 ${
                missingRequired ? "border-status-yellow/60" : "border-border"
              }`}
            >
              <div className="text-xs font-medium text-text-primary">
                {index + 1}. {question}
              </div>
              {options.length > 0 && (
                <div className="flex flex-wrap gap-2">
                  {options.map((opt: any) => {
                    const label = String(opt?.label || opt?.id || "").trim();
                    if (!label) return null;
                    const selected = answers[index] === label;
                    return (
                      <button
                        key={`${index}-${opt?.id || label}`}
                        type="button"
                        onClick={() => setAnswer(index, label)}
                        className={`rounded-full border px-3 py-1 text-xs transition-colors ${
                          selected
                            ? "border-accent bg-accent/15 text-accent"
                            : "border-border bg-bg-3 text-text-secondary hover:border-accent/50"
                        }`}
                      >
                        {label}
                      </button>
                    );
                  })}
                </div>
              )}
              <textarea
                value={answers[index] ?? ""}
                onChange={(e) => setAnswer(index, e.target.value)}
                rows={options.length > 0 ? 2 : 3}
                className="w-full rounded border border-border bg-bg-3 px-3 py-2 text-sm text-text-primary outline-none focus:border-accent"
                placeholder={placeholder}
              />
            </div>
          );
        })}
      </div>
      {submit.error && (
        <p className="text-xs text-status-red">
          Could not send answers:{" "}
          {submit.error instanceof Error ? submit.error.message : "error"}
        </p>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => submit.mutate()}
          disabled={busy || !ready}
          className="rounded bg-accent px-3 py-2 text-xs font-medium text-bg-0 disabled:opacity-60"
        >
          <i className={`fa-solid ${sent ? "fa-spinner fa-spin" : "fa-play"} mr-1.5`} />
          {sent ? "Resuming…" : "Send answers and resume"}
        </button>
        <span className="text-xs text-text-dim">
          {sent
            ? "Answers sent — the pipeline is restarting."
            : ready
              ? "Ready to send."
              : missing.length > 0
                ? `Pick an answer for: ${missing.slice(0, 2).join("; ")}${
                    missing.length > 2 ? "…" : ""
                  }`
                : "Answer the highlighted questions to continue."}
        </span>
      </div>
    </section>
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
