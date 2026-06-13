import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  api,
  HttpError,
  type BudgetView,
  type ExecutionBackendView,
  type GithubScoutConfig,
  type MetaStatus,
  type SkillHubCatalog,
} from "../api/client";
import { PageHeader, PanelCard, PanelHeader } from "../components/Panel";
import StatusPill from "../components/StatusPill";
import MetricCard, { StatRow } from "../components/MetricCard";
import Sparkline from "../components/Sparkline";

/* =============================================================================
   SettingsPage — operator setup & integration console.

   Intentionally NOT another card-stack page. A sticky left rail anchors the
   four configuration domains (Backends, Integrations, Agents, Autonomy) with
   a live readiness glyph each; the right column is a single scroll-spied
   column of sections. This gives Settings its own spatial identity inside the
   Command Center Atelier without inventing new chrome.

   Every write is honest: 409 / 400 / 503 surface verbatim, and the LLM
   key panel renders read-only .env guidance because the backend has no
   key-write endpoint (only PATCH /api/execution/backend persists env today).
   ========================================================================== */

type SectionId = "backends" | "budget" | "integrations" | "agents" | "autonomy";

const SECTIONS: Array<{ id: SectionId; label: string; icon: string; blurb: string }> = [
  { id: "backends", label: "LLM & Sandbox", icon: "fa-solid fa-microchip", blurb: "Backends · execution · keys" },
  { id: "budget", label: "Budget & Cost", icon: "fa-solid fa-dollar-sign", blurb: "Spend cap · per-app max" },
  { id: "integrations", label: "Skills & Scout", icon: "fa-solid fa-puzzle-piece", blurb: "Hub · install · GitHub scout" },
  { id: "agents", label: "Agents", icon: "fa-solid fa-robot", blurb: "Forge new operators" },
  { id: "autonomy", label: "Autonomy", icon: "fa-solid fa-tower-broadcast", blurb: "Meta-cognition loop" },
];

/** Turn any thrown value (HttpError, Error, unknown) into an honest message. */
function errText(err: unknown, fallback = "request failed"): string {
  if (err instanceof HttpError) {
    // Try to lift a JSON {error|detail} message out of the body.
    try {
      const parsed = JSON.parse(err.body) as { error?: string; detail?: string };
      const msg = parsed.error || parsed.detail;
      if (msg) return `${err.status} · ${msg}`;
    } catch {
      /* body wasn't JSON */
    }
    return `${err.status} · ${err.body.slice(0, 160) || "request failed"}`;
  }
  if (err instanceof Error) return err.message;
  return fallback;
}

export default function SettingsPage() {
  const [active, setActive] = useState<SectionId>("backends");

  // Scroll-spy: highlight the rail item whose section is centered in view.
  const refs = useRef<Record<SectionId, HTMLElement | null>>({
    backends: null,
    budget: null,
    integrations: null,
    agents: null,
    autonomy: null,
  });

  useEffect(() => {
    const obs = new IntersectionObserver(
      (entries) => {
        const visible = entries
          .filter((e) => e.isIntersecting)
          .sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
        if (visible?.target instanceof HTMLElement) {
          const id = visible.target.dataset.section as SectionId | undefined;
          if (id) setActive(id);
        }
      },
      { rootMargin: "-20% 0px -55% 0px", threshold: [0.1, 0.5, 1] },
    );
    Object.values(refs.current).forEach((el) => el && obs.observe(el));
    return () => obs.disconnect();
  }, []);

  function jump(id: SectionId) {
    setActive(id);
    refs.current[id]?.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  return (
    <div className="space-y-4">
      <PageHeader
        title="Settings"
        subtitle="Configure backends, skills, agents and the autonomy loop. Writes persist to the orchestrator; key-only settings are documented for .env."
        aside={
          <span className="text-[0.6rem] uppercase tracking-[0.16em] text-text-dim font-mono">
            Operator console
          </span>
        }
      />

      <div className="grid gap-4 lg:grid-cols-[230px_minmax(0,1fr)] items-start">
        {/* ---- Left rail: sticky section nav ---- */}
        <nav
          aria-label="Settings sections"
          className="lg:sticky lg:top-4 panel-card p-2 flex lg:flex-col gap-1 overflow-x-auto"
        >
          <div className="section-label px-2 py-1 hidden lg:block">Sections</div>
          {SECTIONS.map((s) => {
            const isActive = active === s.id;
            return (
              <button
                key={s.id}
                type="button"
                onClick={() => jump(s.id)}
                aria-current={isActive ? "true" : undefined}
                className={[
                  "group relative flex items-center gap-3 rounded-md px-3 py-2 text-left transition-colors shrink-0",
                  "focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/60",
                  isActive
                    ? "bg-accent-soft text-accent border border-accent-line"
                    : "text-text-secondary hover:bg-accent-soft/50 hover:text-text-primary border border-transparent",
                ].join(" ")}
              >
                {isActive && (
                  <span className="absolute left-0 top-1/2 -translate-y-1/2 h-5 w-[3px] rounded-r bg-accent shadow-glow-sm" />
                )}
                <i className={[s.icon, "w-4 text-center text-sm opacity-80"].join(" ")} />
                <span className="min-w-0">
                  <span className="block text-sm font-medium leading-tight">{s.label}</span>
                  <span className="block text-[0.62rem] text-text-dim truncate hidden lg:block">
                    {s.blurb}
                  </span>
                </span>
              </button>
            );
          })}
        </nav>

        {/* ---- Right column: stacked sections ---- */}
        <div className="space-y-6 min-w-0">
          <Section id="backends" refs={refs}>
            <BackendsSection />
          </Section>
          <Section id="budget" refs={refs}>
            <BudgetSection />
          </Section>
          <Section id="integrations" refs={refs}>
            <IntegrationsSection />
          </Section>
          <Section id="agents" refs={refs}>
            <AgentsSection />
          </Section>
          <Section id="autonomy" refs={refs}>
            <AutonomySection />
          </Section>
        </div>
      </div>
    </div>
  );
}

function Section({
  id,
  refs,
  children,
}: {
  id: SectionId;
  refs: React.MutableRefObject<Record<SectionId, HTMLElement | null>>;
  children: React.ReactNode;
}) {
  return (
    <section
      data-section={id}
      ref={(el) => {
        refs.current[id] = el;
      }}
      className="scroll-mt-4 space-y-4"
    >
      {children}
    </section>
  );
}

/* small shared atoms ------------------------------------------------------- */

function LoadingRow({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="px-4 py-6 text-text-secondary text-sm flex items-center gap-2">
      <i className="fa-solid fa-arrows-rotate animate-spin text-accent/70" />
      {label}
    </div>
  );
}

function ErrorRow({ err }: { err: unknown }) {
  return (
    <div className="px-4 py-4 text-sm text-status-red border border-status-red/30 bg-status-red/5 rounded m-4">
      <i className="fa-solid fa-triangle-exclamation mr-2" />
      {errText(err)}
    </div>
  );
}

function EmptyRow({ icon = "fa-solid fa-inbox", label }: { icon?: string; label: string }) {
  return (
    <div className="px-4 py-8 text-center text-text-dim text-sm">
      <i className={[icon, "text-2xl opacity-40 block mb-2"].join(" ")} />
      {label}
    </div>
  );
}

/* =============================================================================
   SECTION 1 — LLM Backends, Execution Sandbox, .env key guidance
   ========================================================================== */

const KEY_GUIDANCE: Array<{ backend: string; env: string }> = [
  { backend: "anthropic", env: "ANTHROPIC_API_KEY" },
  { backend: "openrouter", env: "OPENROUTER_API_KEY" },
  { backend: "openai_cli", env: "OPENAI_API_KEY" },
];

function BackendsSection() {
  const backendsQ = useQuery({ queryKey: ["llm_backends"], queryFn: api.llmBackends });
  const execQ = useQuery({ queryKey: ["execution_backend"], queryFn: api.executionBackend });

  return (
    <>
      <PanelCard>
        <PanelHeader
          title="LLM backends"
          icon="fa-solid fa-microchip"
          description="Backends the orchestrator can route to. Selection is per-stage on the Agents page; this is the available set."
          actions={
            <StatusPill
              status={backendsQ.isLoading ? "pending" : "online"}
              label={
                backendsQ.data ? `${backendsQ.data.length} backends` : backendsQ.isLoading ? "loading" : "—"
              }
            />
          }
        />
        {backendsQ.isLoading && <LoadingRow />}
        {backendsQ.isError && <ErrorRow err={backendsQ.error} />}
        {backendsQ.data && backendsQ.data.length === 0 && (
          <EmptyRow icon="fa-solid fa-plug-circle-xmark" label="No backends reported by the orchestrator." />
        )}
        {backendsQ.data && backendsQ.data.length > 0 && (
          <div className="p-4 flex flex-wrap gap-2">
            {backendsQ.data.map((b) => (
              <span
                key={b}
                className="font-mono text-xs px-2.5 py-1 rounded border border-border bg-bg-3 text-chrome-bright flex items-center gap-1.5"
              >
                <i className="fa-solid fa-circle-nodes text-accent/60 text-[0.6rem]" />
                {b}
              </span>
            ))}
          </div>
        )}
      </PanelCard>

      {/* Execution sandbox — reuses existing /api/execution/backend (the ONLY
          env-persisting endpoint today). */}
      {execQ.isLoading && (
        <PanelCard>
          <LoadingRow label="Loading execution sandbox…" />
        </PanelCard>
      )}
      {execQ.isError && (
        <PanelCard>
          <ErrorRow err={execQ.error} />
        </PanelCard>
      )}
      {execQ.data && <ExecutionSandboxCard view={execQ.data} />}

      <EnvKeyGuidanceCard />
    </>
  );
}

function ExecutionSandboxCard({ view }: { view: ExecutionBackendView }) {
  const qc = useQueryClient();
  const [draft, setDraft] = useState(view.configured);
  const save = useMutation({
    mutationFn: (backend: string) => api.patchExecutionBackend(backend),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["execution_backend"] }),
  });

  useEffect(() => setDraft(view.configured), [view.configured]);

  const dockerHint = view.docker_available
    ? "Docker is running — auto selects the pooled sandbox."
    : "Docker not detected — auto falls back to inline execution.";

  return (
    <PanelCard>
      <PanelHeader
        title="Execution sandbox"
        icon="fa-solid fa-box"
        description={
          <>
            Isolates CodeAgent Python runs. Persisted to{" "}
            <code className="font-mono text-xs bg-bg-3 px-1 rounded">SKYN3T_EXECUTION_BACKEND</code> in .env.{" "}
            {dockerHint}
          </>
        }
        actions={
          <div className="text-right text-xs font-mono text-text-dim">
            <div>resolved · {view.resolved}</div>
            {view.auto_retry && <div className="text-accent mt-0.5">auto-retry on</div>}
          </div>
        }
      />
      <div className="px-4 py-3 flex flex-wrap items-center gap-3">
        <label htmlFor="exec-backend" className="sr-only">
          Execution backend
        </label>
        <select
          id="exec-backend"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          className="min-w-[180px] data-input font-mono focus-visible:ring-2 focus-visible:ring-accent/60"
        >
          {view.valid_backends.map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </select>
        <button
          type="button"
          disabled={save.isPending || draft === view.configured}
          onClick={() => save.mutate(draft)}
          className="btn-ghost"
        >
          {save.isPending ? "saving…" : "save to .env"}
        </button>
        {save.isError && <span className="text-status-red text-xs">{errText(save.error, "save failed")}</span>}
        {save.isSuccess && !save.isPending && (
          <span className="text-status-green text-xs">
            <i className="fa-solid fa-check mr-1" />
            saved
          </span>
        )}
      </div>
    </PanelCard>
  );
}

function EnvKeyGuidanceCard() {
  return (
    <PanelCard>
      <PanelHeader
        title="Provider API keys"
        icon="fa-solid fa-key"
        description="Keys are read from the environment at startup."
        actions={<StatusPill status="disabled" label="read-only" />}
      />
      <div className="p-4 space-y-3">
        <div className="rounded border border-amber-line bg-amber-soft px-3 py-2 text-xs text-amber-strong flex gap-2">
          <i className="fa-solid fa-circle-info mt-0.5" />
          <span>
            There is no UI endpoint to write API keys today. Set them in{" "}
            <code className="font-mono bg-bg-3 px-1 rounded">.env</code> and restart the orchestrator. A{" "}
            <code className="font-mono bg-bg-3 px-1 rounded">PATCH /api/llm/keys</code> endpoint is required to
            enable in-app key management.
          </span>
        </div>
        <div className="grid gap-2 sm:grid-cols-2">
          {KEY_GUIDANCE.map((k) => (
            <div
              key={k.env}
              className="flex items-center justify-between gap-3 rounded border border-border bg-bg-3/60 px-3 py-2"
            >
              <span className="text-sm text-text-secondary">{k.backend}</span>
              <code className="font-mono text-xs text-accent">{k.env}</code>
            </div>
          ))}
        </div>
        <p className="text-[0.7rem] text-text-dim">
          Example: <code className="font-mono bg-bg-3 px-1 rounded">echo "ANTHROPIC_API_KEY=sk-…" &gt;&gt; .env</code>{" "}
          then restart.
        </p>
      </div>
    </PanelCard>
  );
}

/* =============================================================================
   SECTION 1b — Budget & cost caps (spend amount + per-app maximum)

   Two settings, one card: the daily spend budget the autonomy loop may use,
   and the hard cost cap for a single app build. Both persist to .env via
   PATCH /api/budget; "reset to defaults" restores the field defaults. A value
   of 0 means "unlimited" — surfaced inline so the operator isn't surprised.
   ========================================================================== */

function BudgetSection() {
  const budgetQ = useQuery({ queryKey: ["budget"], queryFn: api.budget });

  return (
    <PanelCard>
      <PanelHeader
        title="Budget & cost caps"
        icon="fa-solid fa-dollar-sign"
        description={
          <>
            Set a dollar budget and the maximum price per app build. Persisted to{" "}
            <code className="font-mono text-xs bg-bg-3 px-1 rounded">.env</code> and applied on the next build.{" "}
            <span className="text-text-dim">Use 0 for unlimited.</span>
          </>
        }
        actions={
          <StatusPill
            status={budgetQ.isError ? "disabled" : budgetQ.isLoading ? "pending" : "online"}
            label={
              budgetQ.data
                ? `$${fmtUsd(budgetQ.data.daily_budget_usd)}/day · $${fmtUsd(budgetQ.data.max_build_cost_usd)}/app`
                : budgetQ.isError
                  ? "error"
                  : budgetQ.isLoading
                    ? "loading"
                    : "—"
            }
          />
        }
      />
      {budgetQ.isLoading && <LoadingRow label="Loading budget…" />}
      {budgetQ.isError && <ErrorRow err={budgetQ.error} />}
      {budgetQ.data && <BudgetForm view={budgetQ.data} />}
    </PanelCard>
  );
}

/** Trim trailing zeros for display: 5 -> "5", 0.5 -> "0.5", 1.25 -> "1.25". */
function fmtUsd(n: number): string {
  if (!Number.isFinite(n)) return "0";
  return String(Math.round(n * 100) / 100);
}

function DollarInput({
  id,
  label,
  hint,
  value,
  onChange,
  invalid,
  errorId,
}: {
  id: string;
  label: string;
  hint: string;
  value: string;
  onChange: (v: string) => void;
  invalid: boolean;
  /** id of the shared validation message, announced when this field is invalid */
  errorId?: string;
}) {
  // Announce the hint always, and the constraint error when invalid, so a
  // screen-reader user hears why the field is flagged (matches AgentsSection).
  const describedBy =
    [`${id}-hint`, invalid && errorId ? errorId : null].filter(Boolean).join(" ") || undefined;
  return (
    <div className="space-y-1.5">
      <label htmlFor={id} className="section-label block">
        {label}
      </label>
      <div className="relative">
        <span className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-text-dim font-mono text-sm">
          $
        </span>
        <input
          id={id}
          type="number"
          min={0}
          step="0.5"
          inputMode="decimal"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          aria-invalid={invalid}
          aria-describedby={describedBy}
          className={[
            "w-full data-input font-mono pl-7 focus-visible:ring-2 focus-visible:ring-accent/60",
            invalid ? "ring-1 ring-status-red/60" : "",
          ].join(" ")}
        />
      </div>
      <p id={`${id}-hint`} className="text-[0.65rem] text-text-dim">
        {hint}
      </p>
    </div>
  );
}

function BudgetForm({ view }: { view: BudgetView }) {
  const qc = useQueryClient();
  const [daily, setDaily] = useState(String(view.daily_budget_usd));
  const [perApp, setPerApp] = useState(String(view.max_build_cost_usd));

  // Re-sync local drafts whenever the server value changes (after save/reset).
  useEffect(() => {
    setDaily(String(view.daily_budget_usd));
    setPerApp(String(view.max_build_cost_usd));
  }, [view.daily_budget_usd, view.max_build_cost_usd]);

  const save = useMutation({
    mutationFn: (payload: { daily_budget_usd?: number; max_build_cost_usd?: number; reset?: boolean }) =>
      api.patchBudget(payload),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["budget"] }),
  });

  const dailyNum = Number(daily);
  const perAppNum = Number(perApp);
  const dailyValid = daily.trim() !== "" && Number.isFinite(dailyNum) && dailyNum >= 0;
  const perAppValid = perApp.trim() !== "" && Number.isFinite(perAppNum) && perAppNum >= 0;
  const allValid = dailyValid && perAppValid;
  const dirty = dailyNum !== view.daily_budget_usd || perAppNum !== view.max_build_cost_usd;

  // Distinguish the reset action from a save so only the right button spins.
  const resetting = save.isPending && Boolean(save.variables?.reset);
  const saving = save.isPending && !save.variables?.reset;

  return (
    <div className="p-4 space-y-4">
      <div className="grid gap-4 sm:grid-cols-2">
        <DollarInput
          id="budget-daily"
          label="Spend budget (per day)"
          hint="Total the autonomy loop may spend each day. 0 = unlimited."
          value={daily}
          onChange={setDaily}
          invalid={!dailyValid}
          errorId="budget-validation-error"
        />
        <DollarInput
          id="budget-per-app"
          label="Max price per app"
          hint="Hard cost cap for a single app build. 0 = unlimited."
          value={perApp}
          onChange={setPerApp}
          invalid={!perAppValid}
          errorId="budget-validation-error"
        />
      </div>

      <div className="flex flex-wrap items-center gap-3 pt-1 border-t border-border">
        <button
          type="button"
          className="btn-primary mt-3"
          disabled={save.isPending || !allValid || !dirty}
          onClick={() => save.mutate({ daily_budget_usd: dailyNum, max_build_cost_usd: perAppNum })}
        >
          {saving ? "saving…" : "save to .env"}
        </button>
        <button
          type="button"
          className="btn-ghost mt-3"
          disabled={save.isPending}
          onClick={() => {
            if (window.confirm("Reset spend budget and max price per app to defaults?")) {
              save.mutate({ reset: true });
            }
          }}
        >
          <i className="fa-solid fa-rotate-left mr-1.5" />
          {resetting ? "resetting…" : "reset to defaults"}
        </button>
        <span className="text-[0.7rem] text-text-dim font-mono mt-3">
          defaults · ${fmtUsd(view.defaults.daily_budget_usd)}/day · ${fmtUsd(view.defaults.max_build_cost_usd)}/app
        </span>
        {!allValid && (
          <span id="budget-validation-error" className="text-status-yellow text-xs mt-3">
            <i className="fa-solid fa-circle-exclamation mr-1" />
            enter an amount ≥ 0
          </span>
        )}
        {save.isError && (
          <span className="text-status-red text-xs mt-3">{errText(save.error, "save failed")}</span>
        )}
        {/* Only honest while the draft still matches what was persisted — once
            the operator edits again, the confirmation must clear. */}
        {save.isSuccess && !save.isPending && !dirty && (
          <span className="text-status-green text-xs mt-3">
            <i className="fa-solid fa-check mr-1" />
            {save.data?.reset ? "reset to defaults" : "saved"}
          </span>
        )}
      </div>
    </div>
  );
}

/* =============================================================================
   SECTION 2 — Skills hub, install (path/git), installed list, GitHub scout
   ========================================================================== */

function IntegrationsSection() {
  return (
    <>
      <SkillsHubCard />
      <InstallSkillCard />
      <InstalledSkillsCard />
      <GithubScoutCard />
    </>
  );
}

function SkillsHubCard() {
  const qc = useQueryClient();
  const hubQ = useQuery({ queryKey: ["skills_hub"], queryFn: api.skillsHub });
  const [onlyMissing, setOnlyMissing] = useState(true);

  const install = useMutation({
    mutationFn: (om: boolean) => api.installFromHub(om),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["skills_hub"] });
      qc.invalidateQueries({ queryKey: ["skills_list"] });
    },
  });

  return (
    <PanelCard>
      <PanelHeader
        title="Skill hub"
        icon="fa-solid fa-puzzle-piece"
        description="Curated skill packs bundled with the platform. Install the catalog into the active operator memory."
        actions={
          <StatusPill
            status={hubQ.isLoading ? "pending" : "online"}
            label={hubQ.data ? `${hubQ.data.total} available` : hubQ.isLoading ? "loading" : "—"}
          />
        }
      />
      {hubQ.isLoading && <LoadingRow label="Loading hub catalog…" />}
      {hubQ.isError && <ErrorRow err={hubQ.error} />}
      {hubQ.data && <HubCatalogBody catalog={hubQ.data} />}

      <div className="px-4 py-3 border-t border-border flex flex-wrap items-center gap-3">
        <label className="flex items-center gap-2 text-xs text-text-secondary cursor-pointer select-none">
          <input
            type="checkbox"
            checked={onlyMissing}
            onChange={(e) => setOnlyMissing(e.target.checked)}
            className="accent-accent w-3.5 h-3.5"
          />
          only install missing
        </label>
        <button
          type="button"
          className="btn-primary"
          disabled={install.isPending || !hubQ.data || hubQ.data.total === 0}
          onClick={() => install.mutate(onlyMissing)}
        >
          {install.isPending ? "installing…" : "install from hub"}
        </button>
        {install.isError && (
          <span className="text-status-red text-xs">{errText(install.error, "install failed")}</span>
        )}
        {install.isSuccess && !install.isPending && (
          <span className="text-status-green text-xs">
            <i className="fa-solid fa-check mr-1" />
            installed {install.data.installed?.length ?? 0}
            {install.data.skipped?.length ? ` · skipped ${install.data.skipped.length}` : ""}
            {install.data.flagged?.length ? ` · ${install.data.flagged.length} flagged` : ""}
          </span>
        )}
      </div>
    </PanelCard>
  );
}

function HubCatalogBody({ catalog }: { catalog: SkillHubCatalog }) {
  const markdown = catalog.markdown_skills ?? [];
  const dirs = catalog.agent_skill_dirs ?? [];
  if (markdown.length === 0 && dirs.length === 0) {
    return <EmptyRow icon="fa-solid fa-box-open" label="Hub catalog is empty." />;
  }
  return (
    <div className="p-4 grid gap-4 sm:grid-cols-2">
      <CatalogColumn title="Markdown skills" items={markdown} icon="fa-solid fa-file-lines" />
      <CatalogColumn title="Agent skill dirs" items={dirs} icon="fa-solid fa-folder-tree" />
    </div>
  );
}

function CatalogColumn({ title, items, icon }: { title: string; items: string[]; icon: string }) {
  return (
    <div className="min-w-0">
      <div className="section-label mb-2 flex items-center gap-1.5">
        <i className={[icon, "text-accent/70"].join(" ")} />
        {title}
        <span className="text-text-dim font-mono normal-case tracking-normal">({items.length})</span>
      </div>
      {items.length === 0 ? (
        <p className="text-text-dim text-xs">none</p>
      ) : (
        <ul className="space-y-1 max-h-44 overflow-y-auto pr-1">
          {items.map((it) => (
            <li
              key={it}
              className="font-mono text-xs text-text-secondary truncate px-2 py-1 rounded bg-bg-3/50 border border-border/50"
              title={it}
            >
              {it}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function InstallSkillCard() {
  const qc = useQueryClient();
  const [source, setSource] = useState("");
  const install = useMutation({
    mutationFn: (src: string) => api.installSkill(src),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["skills_list"] });
      qc.invalidateQueries({ queryKey: ["skills_hub"] });
    },
  });

  const trimmed = source.trim();
  // The mutation result is honest: a 200 body can still carry {error}.
  const resultErr = install.data?.error;

  return (
    <PanelCard>
      <PanelHeader
        title="Install a skill"
        icon="fa-solid fa-download"
        description="Provide a local path or a git URL. The orchestrator validates and registers it; flagged content is reported."
      />
      <form
        className="px-4 py-3 flex flex-wrap items-center gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          if (trimmed) install.mutate(trimmed);
        }}
      >
        <label htmlFor="skill-source" className="sr-only">
          Skill path or git URL
        </label>
        <input
          id="skill-source"
          value={source}
          onChange={(e) => setSource(e.target.value)}
          placeholder="./skills/my-skill  or  https://github.com/org/repo.git"
          className="flex-1 min-w-[16rem] data-input font-mono focus-visible:ring-2 focus-visible:ring-accent/60"
        />
        <button type="submit" className="btn-ghost" disabled={install.isPending || !trimmed}>
          {install.isPending ? "installing…" : "install"}
        </button>
      </form>
      {(install.isError || resultErr || install.isSuccess) && (
        <div className="px-4 pb-3 text-xs space-y-1">
          {install.isError && <p className="text-status-red">{errText(install.error, "install failed")}</p>}
          {resultErr && (
            <p className="text-status-red">
              <i className="fa-solid fa-triangle-exclamation mr-1" />
              {resultErr}
              {install.data?.flagged?.length ? ` · flagged: ${install.data.flagged.join(", ")}` : ""}
            </p>
          )}
          {install.isSuccess && !resultErr && (
            <p className="text-status-green">
              <i className="fa-solid fa-check mr-1" />
              installed {install.data?.installed ?? trimmed}
              {install.data?.warnings?.length ? ` · ${install.data.warnings.length} warning(s)` : ""}
            </p>
          )}
        </div>
      )}
    </PanelCard>
  );
}

function InstalledSkillsCard() {
  const qc = useQueryClient();
  const listQ = useQuery({ queryKey: ["skills_list"], queryFn: () => api.skillsList() });

  const del = useMutation({
    mutationFn: (name: string) => api.deleteSkill(name),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["skills_list"] }),
  });

  const skills = listQ.data?.skills ?? [];

  return (
    <PanelCard>
      <PanelHeader
        title="Installed skills"
        icon="fa-solid fa-list-check"
        description="Registered skills with their reinforcement scores. Remove obsolete ones."
        actions={
          <StatusPill
            status={listQ.isLoading ? "pending" : "online"}
            label={`${skills.length} installed`}
          />
        }
      />
      {listQ.isLoading && <LoadingRow label="Loading skills…" />}
      {listQ.isError && <ErrorRow err={listQ.error} />}
      {!listQ.isLoading && !listQ.isError && skills.length === 0 && (
        <EmptyRow icon="fa-solid fa-graduation-cap" label="No skills installed yet. Install from the hub above." />
      )}
      {skills.length > 0 && (
        <ul className="divide-y divide-border/60">
          {skills.map((s) => (
            <li key={s.slug || s.name} className="px-4 py-2.5 flex items-center gap-3">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2 min-w-0">
                  <span className="text-sm text-text-primary truncate">{s.name}</span>
                  <span className="font-mono text-[0.6rem] text-text-dim shrink-0">{s.source}</span>
                </div>
                {s.description && (
                  <p className="text-xs text-text-dim truncate">{s.description}</p>
                )}
                {s.tags.length > 0 && (
                  <div className="flex flex-wrap gap-1 mt-1">
                    {s.tags.slice(0, 5).map((t) => (
                      <span
                        key={t}
                        className="text-[0.6rem] font-mono px-1.5 py-0.5 rounded bg-accent-soft text-accent border border-accent-line"
                      >
                        {t}
                      </span>
                    ))}
                  </div>
                )}
              </div>
              <div className="text-right shrink-0">
                <div className="font-mono text-sm text-accent">{s.score.toFixed(2)}</div>
                <div className="text-[0.6rem] text-text-dim font-mono">
                  <span className="text-status-green">{s.success_count}✓</span>{" "}
                  <span className="text-status-red">{s.failure_count}✗</span>
                </div>
              </div>
              <button
                type="button"
                aria-label={`Delete skill ${s.name}`}
                disabled={del.isPending && del.variables === s.name}
                onClick={() => {
                  if (window.confirm(`Delete skill "${s.name}"?`)) del.mutate(s.name);
                }}
                className="shrink-0 w-8 h-8 rounded border border-border text-text-dim hover:text-status-red hover:border-status-red/50 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-status-red/50 disabled:opacity-40"
              >
                <i className={del.isPending && del.variables === s.name ? "fa-solid fa-spinner animate-spin" : "fa-solid fa-trash-can"} />
              </button>
            </li>
          ))}
        </ul>
      )}
      {del.isError && (
        <p className="px-4 py-2 text-xs text-status-red border-t border-border">
          {errText(del.error, "delete failed")}
        </p>
      )}
    </PanelCard>
  );
}

function GithubScoutCard() {
  const cfgQ = useQuery({ queryKey: ["github_scout_config"], queryFn: api.githubScoutConfig });
  const [limit, setLimit] = useState<number | null>(null);

  const run = useMutation({
    mutationFn: (payload: { limit?: number }) => api.runGithubScout(payload),
  });

  const cfg = cfgQ.data;
  const effLimit = limit ?? cfg?.default_limit ?? 3;

  return (
    <PanelCard>
      <PanelHeader
        title="GitHub scout"
        icon="fa-brands fa-github"
        description="Autonomous repository discovery. Config is read-only here; trigger a scouting run on demand."
        actions={
          cfg ? <StatusPill status="online" label={cfg.mode} /> : <StatusPill status="pending" label="loading" />
        }
      />
      {cfgQ.isLoading && <LoadingRow label="Loading scout config…" />}
      {cfgQ.isError && <ErrorRow err={cfgQ.error} />}
      {cfg && (
        <div className="p-4 space-y-3">
          <ConfigReadout cfg={cfg} />
          <div className="flex flex-wrap items-center gap-3 pt-1">
            <label htmlFor="scout-limit" className="text-xs text-text-secondary">
              limit
            </label>
            <input
              id="scout-limit"
              type="number"
              min={1}
              max={10}
              value={effLimit}
              onChange={(e) => setLimit(Math.max(1, Math.min(10, Number(e.target.value) || 1)))}
              className="w-20 data-input font-mono focus-visible:ring-2 focus-visible:ring-accent/60"
            />
            <button
              type="button"
              className="btn-ghost"
              disabled={run.isPending}
              onClick={() => run.mutate({ limit: effLimit })}
            >
              {run.isPending ? "starting…" : "run scout"}
            </button>
            {run.isError && <span className="text-status-red text-xs">{errText(run.error, "run failed")}</span>}
            {run.data?.error && <span className="text-status-red text-xs">{run.data.error}</span>}
            {run.isSuccess && !run.data?.error && (
              <span className="text-status-green text-xs">
                <i className="fa-solid fa-check mr-1" />
                {run.data.started ? "started" : run.data.state ?? "queued"}
              </span>
            )}
          </div>
        </div>
      )}
    </PanelCard>
  );
}

function ConfigReadout({ cfg }: { cfg: GithubScoutConfig }) {
  return (
    <div className="grid gap-2 sm:grid-cols-2">
      <StatRow label="Mode" value={<span className="font-mono">{cfg.mode}</span>} />
      <StatRow label="Default limit" value={<span className="font-mono">{cfg.default_limit}</span>} />
      <div className="sm:col-span-2">
        <div className="section-label mb-1.5">Discovery lanes</div>
        <div className="flex flex-wrap gap-1.5">
          {cfg.discovery_lanes.length === 0 ? (
            <span className="text-text-dim text-xs">none</span>
          ) : (
            cfg.discovery_lanes.map((l) => (
              <span
                key={l}
                className="font-mono text-[0.65rem] px-2 py-0.5 rounded bg-bg-3 border border-border text-chrome-bright"
              >
                {l}
              </span>
            ))
          )}
        </div>
      </div>
      {cfg.summary && <p className="sm:col-span-2 text-xs text-text-dim">{cfg.summary}</p>}
    </div>
  );
}

/* =============================================================================
   SECTION 3 — Forge a new agent from a base type
   ========================================================================== */

function AgentsSection() {
  const qc = useQueryClient();
  const typesQ = useQuery({ queryKey: ["agent_types"], queryFn: api.agentTypes });

  const [name, setName] = useState("");
  const [baseType, setBaseType] = useState("blank");

  const create = useMutation({
    mutationFn: (payload: { name: string; base_type: string }) => api.createAgent(payload),
    onSuccess: (res) => {
      // Honest: a 200 can still report {error}. Only clear + invalidate on real success.
      if (res.ok) {
        setName("");
        qc.invalidateQueries({ queryKey: ["agents"] });
      }
    },
  });

  // Default the base-type select to the first reported type once loaded.
  useEffect(() => {
    if (typesQ.data && typesQ.data.length > 0 && !typesQ.data.includes(baseType)) {
      setBaseType(typesQ.data[0]);
    }
  }, [typesQ.data, baseType]);

  const trimmed = name.trim();
  const nameValid = /^[A-Za-z0-9_-]+$/.test(trimmed);
  const resultErr = create.data && !create.data.ok ? create.data.error : undefined;

  return (
    <PanelCard>
      <PanelHeader
        title="Create an agent"
        icon="fa-solid fa-robot"
        description="Forge a new operator from a base type. The spec is persisted and the agent registered live."
        actions={
          <StatusPill
            status={typesQ.isLoading ? "pending" : "online"}
            label={typesQ.data ? `${typesQ.data.length} types` : "loading"}
          />
        }
      />
      {typesQ.isError && <ErrorRow err={typesQ.error} />}
      <form
        className="p-4 space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          if (trimmed && nameValid) create.mutate({ name: trimmed, base_type: baseType });
        }}
      >
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-1.5">
            <label htmlFor="agent-name" className="section-label block">
              Name
            </label>
            <input
              id="agent-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="MyResearchAgent"
              aria-invalid={trimmed.length > 0 && !nameValid}
              aria-describedby="agent-name-hint"
              className="w-full data-input font-mono focus-visible:ring-2 focus-visible:ring-accent/60"
            />
            <p id="agent-name-hint" className="text-[0.65rem] text-text-dim">
              Letters, digits, <code className="font-mono">-</code> and <code className="font-mono">_</code> only.
            </p>
          </div>
          <div className="space-y-1.5">
            <label htmlFor="agent-base" className="section-label block">
              Base type
            </label>
            <select
              id="agent-base"
              value={baseType}
              onChange={(e) => setBaseType(e.target.value)}
              disabled={typesQ.isLoading || !typesQ.data}
              className="w-full data-input font-mono focus-visible:ring-2 focus-visible:ring-accent/60"
            >
              {typesQ.isLoading && <option>loading…</option>}
              {(typesQ.data ?? []).map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-3">
          <button type="submit" className="btn-primary" disabled={create.isPending || !trimmed || !nameValid}>
            {create.isPending ? "creating…" : "create agent"}
          </button>
          {trimmed.length > 0 && !nameValid && (
            <span className="text-status-yellow text-xs">
              <i className="fa-solid fa-circle-exclamation mr-1" />
              invalid name
            </span>
          )}
          {create.isError && (
            <span className="text-status-red text-xs">{errText(create.error, "create failed")}</span>
          )}
          {resultErr && (
            <span className="text-status-red text-xs">
              <i className="fa-solid fa-triangle-exclamation mr-1" />
              {resultErr}
            </span>
          )}
          {create.data?.ok && (
            <span className="text-status-green text-xs">
              <i className="fa-solid fa-check mr-1" />
              created {create.data.name}
            </span>
          )}
        </div>
      </form>
    </PanelCard>
  );
}

/* =============================================================================
   SECTION 4 — Autonomy (meta-cognition loop) pause/resume
   ========================================================================== */

function AutonomySection() {
  const qc = useQueryClient();
  const metaQ = useQuery({
    queryKey: ["meta_status"],
    queryFn: api.metaStatus,
    refetchInterval: 8_000,
  });

  const pause = useMutation({
    mutationFn: () => api.pauseMeta(),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["meta_status"] }),
  });
  const resume = useMutation({
    mutationFn: () => api.resumeMeta(),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["meta_status"] }),
  });

  const meta = metaQ.data;
  const busy = pause.isPending || resume.isPending;
  const lastActionErr =
    (pause.data && pause.data.error) || (resume.data && resume.data.error) || undefined;

  return (
    <PanelCard>
      <PanelHeader
        title="Autonomy loop"
        icon="fa-solid fa-tower-broadcast"
        description="The meta-cognition loop observes the swarm and proposes adjustments. Pause it to freeze autonomous actions."
        actions={<AutonomyPill meta={meta} loading={metaQ.isLoading} />}
      />
      {metaQ.isLoading && <LoadingRow label="Loading autonomy status…" />}
      {metaQ.isError && <ErrorRow err={metaQ.error} />}

      {meta && !meta.enabled && (
        <EmptyRow
          icon="fa-solid fa-tower-broadcast"
          label="The meta-cognition loop is disabled in this build. Enable it in the orchestrator to use autonomy controls."
        />
      )}

      {meta && meta.enabled && (
        <div className="p-4 space-y-4">
          <div className="grid gap-3 sm:grid-cols-3">
            <MetricCard
              label="Observations"
              value={meta.observations_collected ?? 0}
              icon="fa-solid fa-eye"
              accent="cyan"
            />
            <MetricCard
              label="Actions taken"
              value={meta.actions_taken ?? 0}
              icon="fa-solid fa-bolt"
              accent="amber"
            />
            <MetricCard
              label="Interval"
              value={meta.interval_seconds != null ? `${meta.interval_seconds}s` : "—"}
              icon="fa-solid fa-stopwatch"
              accent="green"
            />
          </div>

          <RecentActions meta={meta} />

          <div className="flex flex-wrap items-center gap-3 pt-1 border-t border-border">
            <div className="flex items-center gap-2 text-sm pt-3">
              <span className="text-text-secondary">Loop is</span>
              <span className={meta.running ? "text-status-green font-medium" : "text-status-yellow font-medium"}>
                {meta.running ? "running" : "paused"}
              </span>
            </div>
            <div className="pt-3 flex gap-2">
              <button
                type="button"
                className="btn-ghost"
                disabled={busy || !meta.running}
                onClick={() => pause.mutate()}
              >
                <i className="fa-solid fa-pause mr-1.5" />
                {pause.isPending ? "pausing…" : "pause"}
              </button>
              <button
                type="button"
                className="btn-primary"
                disabled={busy || meta.running}
                onClick={() => resume.mutate()}
              >
                <i className="fa-solid fa-play mr-1.5" />
                {resume.isPending ? "resuming…" : "resume"}
              </button>
            </div>
            {(pause.isError || resume.isError) && (
              <span className="text-status-red text-xs pt-3">
                {errText(pause.error ?? resume.error, "control failed")}
              </span>
            )}
            {lastActionErr && <span className="text-status-red text-xs pt-3">{lastActionErr}</span>}
          </div>
        </div>
      )}
    </PanelCard>
  );
}

function AutonomyPill({ meta, loading }: { meta?: MetaStatus; loading: boolean }) {
  if (loading) return <StatusPill status="pending" label="loading" />;
  if (!meta || !meta.enabled) return <StatusPill status="disabled" label="disabled" />;
  return meta.running ? (
    <StatusPill status="running" label="running" pulse />
  ) : (
    <StatusPill status="pending" label="paused" />
  );
}

function RecentActions({ meta }: { meta: MetaStatus }) {
  const actions = meta.recent_actions ?? [];
  // Build a tiny activity trend from action timestamps when present; the
  // Sparkline is the only "live data" element on this otherwise-static page.
  const trend = useMemo<number[]>(() => {
    const out: number[] = [];
    for (const a of actions) {
      const v = typeof a.value === "number" ? a.value : typeof a.score === "number" ? a.score : 1;
      out.push(v);
    }
    return out;
  }, [actions]);

  return (
    <div>
      <div className="flex items-center justify-between mb-2">
        <div className="section-label">Recent actions</div>
        {trend.length > 1 && (
          <span className="text-accent">
            <Sparkline values={trend} aria-label="Recent autonomy action trend" width={120} height={24} />
          </span>
        )}
      </div>
      {actions.length === 0 ? (
        <p className="text-text-dim text-xs">No actions taken yet.</p>
      ) : (
        <ul className="space-y-1 max-h-48 overflow-y-auto pr-1">
          {actions.map((a, i) => {
            const kind = typeof a.kind === "string" ? a.kind : typeof a.type === "string" ? a.type : "action";
            const summary =
              typeof a.summary === "string"
                ? a.summary
                : typeof a.detail === "string"
                  ? a.detail
                  : JSON.stringify(a).slice(0, 120);
            return (
              <li
                key={i}
                className="flex items-start gap-2 text-xs px-2 py-1.5 rounded bg-bg-3/50 border border-border/50"
              >
                <span className="font-mono text-[0.6rem] text-accent uppercase tracking-wider shrink-0 mt-0.5">
                  {kind}
                </span>
                <span className="text-text-secondary min-w-0 truncate">{summary}</span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
