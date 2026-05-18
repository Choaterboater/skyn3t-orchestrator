import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, Skill } from "../api/client";

// First-class view of the skill library — the durable artifacts the
// system writes to data/skills/. Filterable by tag (each skill is
// tagged with its stack plus 'build-success' / 'fix-loop' etc).
export default function SkillsPage() {
  const [tag, setTag] = useState<string>("");
  const { data, isLoading, error } = useQuery({
    queryKey: ["skills", tag],
    queryFn: () => api.skills(tag || undefined),
  });

  const list: Skill[] = tag
    ? data?.skills ?? []
    : data?.top ?? [];
  const summary = data?.summary ?? {};
  const allTags: string[] = summary.tags ?? [];

  return (
    <div className="space-y-6">
      <header>
        <h1 className="display text-4xl">
          <span className="text-accent">Skills</span>
        </h1>
        <p className="text-text-secondary text-sm mt-1">
          Durable learned artifacts in <code className="font-mono bg-bg-3 px-1 rounded">data/skills/</code>.
          Hand-edit them in your editor — the runner picks the changes up on the
          next scan.
        </p>
      </header>

      <div className="flex items-center gap-3">
        <span className="text-sm text-text-secondary">Filter:</span>
        <button
          onClick={() => setTag("")}
          className={`text-xs px-2 py-1 rounded border ${
            tag === ""
              ? "bg-accent-soft text-accent border-accent-line"
              : "bg-bg-2 text-text-secondary border-border hover:border-border-strong"
          }`}
        >
          all
        </button>
        {allTags.map((t) => (
          <button
            key={t}
            onClick={() => setTag(t)}
            className={`text-xs px-2 py-1 rounded border ${
              tag === t
                ? "bg-accent-soft text-accent border-accent-line"
                : "bg-bg-2 text-text-secondary border-border hover:border-border-strong"
            }`}
          >
            {t}
          </button>
        ))}
      </div>

      {isLoading && <p className="text-text-secondary">Loading…</p>}
      {error && (
        <p className="text-status-red text-sm">
          {error instanceof Error ? error.message : "load failed"}
        </p>
      )}

      {!isLoading && list.length === 0 && (
        <p className="text-text-secondary">
          No skills yet — they appear automatically once the system has
          completed a few builds.
        </p>
      )}

      <div className="space-y-3">
        {list.map((s) => (
          <SkillCard key={s.name} skill={s} />
        ))}
      </div>
    </div>
  );
}

function SkillCard({ skill }: { skill: Skill }) {
  const total = skill.success_count + skill.failure_count;
  const scorePct = total === 0 ? "—" : `${Math.round(skill.score * 100)}%`;
  return (
    <div className="rounded-lg border border-border bg-bg-2 p-4">
      <div className="flex items-baseline justify-between gap-4 mb-2">
        <h3 className="font-mono text-accent">{skill.name}</h3>
        <span className="text-xs text-text-dim font-mono">
          score {scorePct} · {skill.success_count}↑ {skill.failure_count}↓
        </span>
      </div>
      <div className="flex flex-wrap gap-1 mb-3">
        {skill.tags.map((t) => (
          <span
            key={t}
            className="text-[0.65rem] px-1.5 py-0.5 rounded border border-border bg-bg-3 text-text-secondary"
          >
            {t}
          </span>
        ))}
      </div>
      <pre className="text-xs text-text-primary whitespace-pre-wrap font-mono leading-relaxed max-h-72 overflow-y-auto">
        {skill.body}
      </pre>
    </div>
  );
}
