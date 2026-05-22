import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  api,
  AgentConfigView,
  AgentRow,
  RoutingRecommendation,
  RoutingRoute,
  RoutingTier,
} from "../api/client";

// Agent registry. Click a row to open the edit drawer — change backend,
// model, system prompt, temperature, max_tokens. Enable/disable inline.
// Long names truncate with a tooltip so the columns don't squish.
export default function AgentsPage() {
  const qc = useQueryClient();
  const [editing, setEditing] = useState<string | null>(null);

  const { data, isLoading, error } = useQuery({
    queryKey: ["agents"],
    queryFn: api.agents,
    refetchInterval: 15_000,
  });

  // Token rollup per agent — keeps a "how much has each one burned"
  // column live. Falls back gracefully if the endpoint is unavailable
  // (older backend without the tracker).
  const usage = useQuery({
    queryKey: ["usage_agents"],
    queryFn: api.usagePerAgent,
    refetchInterval: 10_000,
    retry: false,
  });
  const totals = useQuery({
    queryKey: ["usage_totals"],
    queryFn: api.usageTotals,
    refetchInterval: 10_000,
    retry: false,
  });
  const usageByName = new Map(
    (usage.data ?? []).map((u) => [u.agent, u]),
  );
  const routing = useQuery({
    queryKey: ["routing_policy"],
    queryFn: api.routingPolicy,
    refetchInterval: 15_000,
  });
  const routingRecommendations = useQuery({
    queryKey: ["routing_recommendations"],
    queryFn: api.routingRecommendations,
    refetchInterval: 15_000,
    retry: false,
  });

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["agents"] });
    qc.invalidateQueries({ queryKey: ["routing_policy"] });
    qc.invalidateQueries({ queryKey: ["routing_recommendations"] });
  };

  return (
    <div className="space-y-6">
      <header className="flex items-baseline justify-between gap-4">
        <div>
          <h1 className="display text-4xl">
            <span className="text-accent">Agents</span>
          </h1>
          <p className="text-text-secondary text-sm mt-1">
            Every registered agent. Click a row to edit backend, model, prompt,
            and limits.
          </p>
        </div>
        {totals.data && (
          <div className="text-right text-xs space-y-0.5">
            <div className="text-text-secondary uppercase tracking-wider">
              Total tokens (this session)
            </div>
            <div className="text-2xl font-mono text-accent">
              {fmtTokens(totals.data.total_tokens ?? 0)}
            </div>
            <div className="text-text-dim font-mono">
              {totals.data.total_calls ?? 0} LLM calls ·{" "}
              {totals.data.agents_tracked ?? 0} agents
            </div>
          </div>
        )}
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

      {routing.data && (
        <RoutingPolicyCard
          routes={routing.data.routes ?? []}
          tiers={routing.data.tiers ?? []}
          recommendations={routingRecommendations.data ?? []}
          onChanged={invalidate}
        />
      )}

      <div
        className={[
          "grid gap-5",
          editing ? "grid-cols-[minmax(0,1fr)_420px]" : "grid-cols-1",
        ].join(" ")}
      >
        {data && data.length > 0 && (
          <div className="rounded-lg border border-border bg-bg-2 overflow-hidden min-w-0">
            <table className="w-full text-sm">
              <thead className="bg-bg-3 text-text-secondary text-xs uppercase tracking-wider">
                <tr>
                  <Th>Name</Th>
                  <Th>Type</Th>
                  <Th>Backend</Th>
                  <Th>Model</Th>
                  <Th align="right">Tokens</Th>
                  <Th align="right">Calls</Th>
                  <Th align="right">Status</Th>
                  <Th align="right">Errors</Th>
                  <Th align="right">Actions</Th>
                </tr>
              </thead>
              <tbody>
                {data.map((a) => (
                  <AgentTableRow
                    key={a.name}
                    a={a}
                    selected={editing === a.name}
                    onSelect={() => setEditing(a.name)}
                    onChanged={invalidate}
                    usage={usageByName.get(a.name)}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}

        {editing && (
          <AgentEditDrawer
            name={editing}
            onClose={() => setEditing(null)}
            onChanged={invalidate}
          />
        )}
      </div>
    </div>
  );
}

function RoutingPolicyCard({
  routes,
  tiers,
  recommendations,
  onChanged,
}: {
  routes: RoutingRoute[];
  tiers: RoutingTier[];
  recommendations: RoutingRecommendation[];
  onChanged: () => void;
}) {
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const recByStage = new Map(recommendations.map((rec) => [rec.stage, rec]));
  const save = useMutation({
    mutationFn: ({ stage, tier }: { stage: string; tier: string }) =>
      api.patchRoutingPolicy({ [stage]: { tier, applied_via: "manual" } }),
    onSuccess: () => onChanged(),
  });
  const reset = useMutation({
    mutationFn: (stage: string) => api.resetRoutingPolicy(stage),
    onSuccess: () => onChanged(),
  });

  return (
    <section className="rounded-lg border border-border bg-bg-2 overflow-hidden">
      <header className="px-4 py-3 border-b border-border">
        <h2 className="font-mono text-accent">Routing policy</h2>
        <p className="text-text-secondary text-sm mt-1">
          Stage defaults now come from saved policy first, then env bootstrap, then built-in defaults.
        </p>
        <p className="text-text-dim text-xs mt-1">
          Recommendations are advisory. Saving marks an override manual; apply rec marks it recommendation until you reset it.
        </p>
      </header>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-bg-3 text-text-secondary text-xs uppercase tracking-wider">
            <tr>
              <Th>Stage</Th>
              <Th>Tier</Th>
              <Th>Backend</Th>
              <Th>Model</Th>
              <Th>Source</Th>
              <Th>Recommendation</Th>
              <Th align="right">Actions</Th>
            </tr>
          </thead>
          <tbody>
            {routes.map((route) => {
              const currentTier = drafts[route.stage] ?? route.tier;
              const rec = recByStage.get(route.stage);
              const canApplyRec = !!rec && rec.applyable && rec.recommended_tier !== currentTier;
              const canSave =
                currentTier !== route.tier ||
                (route.source === "persisted" && route.persisted_via === "recommendation");
              return (
                <tr key={route.stage} className="border-t border-border">
                  <Td mono>{route.stage}</Td>
                  <Td>
                    <select
                      value={currentTier}
                      onChange={(e) =>
                        setDrafts((d) => ({ ...d, [route.stage]: e.target.value }))
                      }
                      className="w-full bg-bg-3 border border-border rounded px-2 py-1.5 text-sm font-mono outline-none focus:border-accent"
                    >
                      {tiers.map((tier) => (
                        <option key={tier.name} value={tier.name}>
                          {tier.name}
                        </option>
                      ))}
                    </select>
                  </Td>
                  <Td mono>{route.backend ?? "—"}</Td>
                  <Td mono>{route.model ?? "(backend default)"}</Td>
                  <Td>{routingSourceLabel(route)}</Td>
                  <Td>
                    {rec ? (
                      <div className="space-y-1">
                        <div className="flex items-center gap-2">
                          <span className="font-mono text-xs">
                            {rec.recommended_tier}
                          </span>
                          <span
                            className={[
                              "text-[0.65rem] px-1.5 py-0.5 rounded border uppercase tracking-wider",
                              rec.applyable
                                ? "border-accent-line bg-accent-soft text-accent"
                                : "border-border text-text-secondary",
                            ].join(" ")}
                          >
                            {rec.recommendation_kind} · {rec.confidence}
                          </span>
                        </div>
                        <div className="text-[0.65rem] text-text-dim">
                          {(rec.reasons ?? []).slice(0, 2).join(" ")}
                        </div>
                        {routingEvidenceChips(rec).length > 0 && (
                          <div className="flex flex-wrap gap-1">
                            {routingEvidenceChips(rec).map((chip) => (
                              <span
                                key={chip}
                                className="text-[0.65rem] px-1.5 py-0.5 rounded border border-border bg-bg-3 text-text-secondary font-mono"
                              >
                                {chip}
                              </span>
                            ))}
                          </div>
                        )}
                        {routingEvidenceNote(rec) && (
                          <div className="text-[0.65rem] text-text-secondary">
                            {routingEvidenceNote(rec)}
                          </div>
                        )}
                      </div>
                    ) : (
                      <span className="text-text-dim">—</span>
                    )}
                  </Td>
                  <Td align="right">
                    <div className="inline-flex gap-2">
                      <button
                        type="button"
                        disabled={save.isPending || !canSave}
                        onClick={() => save.mutate({ stage: route.stage, tier: currentTier })}
                        className="text-[0.65rem] px-2 py-0.5 rounded border border-accent-line text-accent hover:bg-accent-soft disabled:opacity-50"
                      >
                        save
                      </button>
                      <button
                        type="button"
                        disabled={save.isPending || !canApplyRec || !rec}
                        onClick={() =>
                          rec &&
                          api
                            .patchRoutingPolicy({
                              [route.stage]: {
                                tier: rec.recommended_tier,
                                applied_via: "recommendation",
                              },
                            })
                            .then(() => onChanged())
                        }
                        className="text-[0.65rem] px-2 py-0.5 rounded border border-accent-line text-accent hover:bg-accent-soft disabled:opacity-50"
                      >
                        apply rec
                      </button>
                      <button
                        type="button"
                        disabled={reset.isPending || route.source !== "persisted"}
                        onClick={() => reset.mutate(route.stage)}
                        className="text-[0.65rem] px-2 py-0.5 rounded border border-border text-text-secondary hover:border-border-strong disabled:opacity-50"
                      >
                        reset
                      </button>
                    </div>
                  </Td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function routingSourceLabel(route: RoutingRoute): string {
  if (route.source !== "persisted") {
    return route.source;
  }
  const appliedVia = route.persisted_via === "recommendation" ? "recommendation" : "manual";
  return `persisted · ${appliedVia}`;
}

function routingEvidenceChips(rec: RoutingRecommendation): string[] {
  const signals = rec.signals ?? {};
  const chips: string[] = [];
  const samples = signals.trajectory_samples ?? 0;
  if (samples > 0) {
    chips.push(`${samples} traj`);
  }
  const latency = signals.avg_latency_seconds ?? 0;
  if (latency > 0) {
    chips.push(`${Math.round(latency)}s avg`);
  }
  const tokenWeight = Math.max(
    signals.trajectory_stage_tokens ?? 0,
    signals.live_stage_tokens ?? 0,
  );
  if (tokenWeight > 0) {
    chips.push(`${fmtTokens(tokenWeight)} tok`);
  }
  const mixed = signals.mixed_route_samples ?? 0;
  if (mixed > 0) {
    chips.push(`${mixed} mixed`);
  }
  return chips;
}

function routingEvidenceNote(rec: RoutingRecommendation): string | null {
  const samples = rec.signals?.trajectory_samples ?? 0;
  if (samples <= 0 && rec.confidence === "low") {
    return "No trajectory evidence yet — this is still a default/heuristic recommendation.";
  }
  const mixed = rec.signals?.mixed_route_samples ?? 0;
  if (mixed > 0) {
    return "Mixed-route stage runs are excluded from per-route scoring, so confidence stays conservative.";
  }
  return null;
}

function displayBackend(a: Pick<AgentRow, "effective_backend" | "backend">): string {
  return a.effective_backend ?? a.backend ?? "—";
}

function displayModel(
  a: Pick<AgentRow, "effective_backend" | "backend" | "effective_model" | "model">,
): string {
  return a.effective_model ?? a.model ?? (displayBackend(a) === "—" ? "—" : "(backend default)");
}

function AgentTableRow({
  a,
  selected,
  onSelect,
  onChanged,
  usage,
}: {
  a: AgentRow;
  selected: boolean;
  onSelect: () => void;
  onChanged: () => void;
  usage?: { total_tokens: number; calls: number };
}) {
  const warnIfPersistFailed = (action: string, res: { persisted?: boolean; persist_error?: string } | undefined) => {
    if (res && res.persisted === false) {
      console.warn(
        `Agent ${a.name}: ${action} succeeded in-memory but persistence failed: ` +
          (res.persist_error ?? "(no detail)"),
      );
    }
  };
  const enable = useMutation({
    mutationFn: () => api.enableAgent(a.name),
    onSuccess: (res) => {
      warnIfPersistFailed("enable", res);
      onChanged();
    },
  });
  const disable = useMutation({
    mutationFn: () => api.disableAgent(a.name),
    onSuccess: (res) => {
      warnIfPersistFailed("disable", res);
      onChanged();
    },
  });
  return (
    <tr
      className={[
        "border-t border-border cursor-pointer",
        selected ? "bg-accent-soft" : "hover:bg-bg-3",
      ].join(" ")}
      onClick={onSelect}
    >
      <Td truncate>{a.name}</Td>
      <Td>{a.agent_type ?? "—"}</Td>
      <Td mono>{displayBackend(a)}</Td>
      <Td mono>{displayModel(a)}</Td>
      <Td align="right" mono>
        {usage ? fmtTokens(usage.total_tokens) : "—"}
      </Td>
      <Td align="right" mono>
        {usage ? usage.calls : "—"}
      </Td>
      <Td align="right">
        <StatusPill status={a.status ?? "unknown"} />
      </Td>
      <Td align="right" mono>
        {a.recent_errors ?? 0}
      </Td>
      <Td align="right">
        <span
          onClick={(e) => e.stopPropagation()}
          className="inline-flex gap-1"
        >
          {a.status === "disabled" ? (
            <button
              onClick={() => enable.mutate()}
              disabled={enable.isPending}
              className="text-[0.65rem] px-2 py-0.5 rounded border border-status-green/40 text-status-green hover:bg-status-green/10"
            >
              enable
            </button>
          ) : (
            <button
              onClick={() => disable.mutate()}
              disabled={disable.isPending}
              className="text-[0.65rem] px-2 py-0.5 rounded border border-border text-text-secondary hover:border-border-strong"
            >
              disable
            </button>
          )}
          <button
            onClick={onSelect}
            className="text-[0.65rem] px-2 py-0.5 rounded border border-accent-line text-accent hover:bg-accent-soft"
          >
            edit
          </button>
        </span>
      </Td>
    </tr>
  );
}

function AgentEditDrawer({
  name,
  onClose,
  onChanged,
}: {
  name: string;
  onClose: () => void;
  onChanged: () => void;
}) {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["agent_config", name],
    queryFn: () => api.agentConfig(name),
  });
  // Backend catalog for the dropdowns. Cached aggressively — backends
  // and their model lists don't change at runtime.
  const catalog = useQuery({
    queryKey: ["agent_models"],
    queryFn: api.agentModels,
    staleTime: 5 * 60_000,
  });
  const [form, setForm] = useState<AgentConfigView["config"]>({});
  const [enabled, setEnabled] = useState<boolean>(true);

  useEffect(() => {
    if (data) {
      setForm({ ...(data.config ?? {}) });
      setEnabled(data.enabled ?? true);
    }
  }, [data]);

  const save = useMutation({
    mutationFn: (patch: Record<string, unknown>) =>
      api.patchAgentConfig(name, patch),
    onSuccess: (res) => {
      if (res && res.persisted === false) {
        // Persistence failed but the in-memory change succeeded. Surface
        // it so operators know the config will revert on restart.
        console.warn(
          `Agent ${name}: in-memory updated but persistence failed: ` +
            (res.persist_error ?? "(no detail)"),
        );
      }
      onChanged();
      refetch();
    },
  });
  const resetLLM = useMutation({
    mutationFn: () => api.resetAgentConfig(name, ["backend", "model"]),
    onSuccess: (res) => {
      if (res && res.persisted === false) {
        console.warn(
          `Agent ${name}: live routing reset but persistence failed: ` +
            (res.persist_error ?? "(no detail)"),
        );
      }
      onChanged();
      refetch();
    },
  });

  const del = useMutation({
    mutationFn: () => api.deleteAgent(name),
    onSuccess: (res) => {
      const failedSteps = res?.errors ?? [];
      const cleanup = res?.cleanup ?? {};
      const partialFailures = Object.entries(cleanup)
        .filter(([, ok]) => ok === false)
        .map(([step]) => step);
      if (failedSteps.length || partialFailures.length) {
        // Partial delete — the agent may be gone from memory but not
        // from the spec store, override file, or registry. Tell the
        // operator so they can re-run or fix the underlying cause.
        const detail = [
          ...partialFailures.map((s) => `${s} cleanup failed`),
          ...failedSteps,
        ].join("; ");
        console.warn(`Agent ${name}: partial delete — ${detail}`);
        alert(
          `Agent "${name}" was partially deleted.\n\n${detail}\n\n` +
            "Some on-disk state may remain. Check server logs.",
        );
      }
      onChanged();
      onClose();
    },
  });

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    save.mutate({ ...form, enabled });
  }

  return (
    <aside className="rounded-lg border border-border bg-bg-2 p-4 space-y-3 max-h-[80vh] overflow-y-auto">
      <header className="flex items-baseline justify-between gap-2 min-w-0">
        <h2 className="font-mono text-accent truncate" title={name}>
          {name}
        </h2>
        <button
          onClick={onClose}
          className="text-text-dim hover:text-text-primary text-sm shrink-0"
        >
          <i className="fa-solid fa-xmark" />
        </button>
      </header>

      {isLoading && <p className="text-text-secondary text-sm">Loading…</p>}
      {error && (
        <p className="text-status-red text-sm">
          {error instanceof Error ? error.message : "load failed"}
        </p>
      )}

      {data && (
        <form onSubmit={onSubmit} className="space-y-3">
          <div className="text-[0.65rem] text-text-dim font-mono">
            {data.agent_type ?? "—"} · {data.provider ?? "—"}
          </div>
          <div className="rounded border border-border bg-bg-3 px-3 py-2 text-xs space-y-1">
            <div className="font-mono text-text-secondary uppercase tracking-wider">
              Effective routing
            </div>
            <div className="font-mono">
              {data.effective_backend ?? "—"} ·{" "}
              {data.effective_model ?? (data.effective_backend ? "(backend default)" : "—")}
            </div>
            <div className="text-text-dim">
              {data.effective_source === "config"
                ? "explicit agent override"
                : data.effective_source === "policy"
                  ? `stage ${data.effective_stage ?? "—"} · ${data.effective_tier ?? "—"} · ${data.effective_policy_source ?? "policy"}`
                  : "no routed model"}
            </div>
          </div>

          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={enabled}
              onChange={(e) => setEnabled(e.target.checked)}
              className="accent-accent"
            />
            Enabled
          </label>

          <BackendModelDropdowns
            backend={form?.backend ?? ""}
            model={form?.model ?? ""}
            catalog={catalog.data?.backends ?? []}
            onChange={(patch) => setForm((f) => ({ ...f, ...patch }))}
          />

          <Field label="System prompt">
            <textarea
              value={form?.system_prompt ?? ""}
              onChange={(e) =>
                setForm((f) => ({ ...f, system_prompt: e.target.value }))
              }
              rows={5}
              className="w-full bg-bg-3 border border-border rounded px-2 py-1.5 text-sm outline-none focus:border-accent resize-y"
            />
          </Field>

          <div className="grid grid-cols-2 gap-2">
            <Field label="Temperature">
              <input
                type="number"
                step="0.05"
                min="0"
                max="2"
                value={form?.temperature ?? ""}
                onChange={(e) =>
                  setForm((f) => ({
                    ...f,
                    temperature:
                      e.target.value === "" ? undefined : Number(e.target.value),
                  }))
                }
                className="w-full bg-bg-3 border border-border rounded px-2 py-1.5 text-sm font-mono outline-none focus:border-accent"
              />
            </Field>
            <Field label="Max tokens">
              <input
                type="number"
                step="100"
                min="0"
                value={form?.max_tokens ?? ""}
                onChange={(e) =>
                  setForm((f) => ({
                    ...f,
                    max_tokens:
                      e.target.value === "" ? undefined : Number(e.target.value),
                  }))
                }
                className="w-full bg-bg-3 border border-border rounded px-2 py-1.5 text-sm font-mono outline-none focus:border-accent"
              />
            </Field>
          </div>

          {Array.isArray(data.capabilities) && data.capabilities.length > 0 && (
            <Field label="Capabilities (read-only)">
              <div className="flex flex-wrap gap-1">
                {data.capabilities.map((c) => (
                  <span
                    key={c}
                    className="text-[0.6rem] px-1.5 py-0.5 rounded border border-border bg-bg-3 text-text-secondary font-mono"
                  >
                    {c}
                  </span>
                ))}
              </div>
            </Field>
          )}

          <div className="flex items-center gap-2 pt-2 border-t border-border">
            <button
              type="submit"
              disabled={save.isPending}
              className="rounded bg-accent text-bg-0 text-sm font-medium px-3 py-1.5 disabled:opacity-60"
            >
              {save.isPending ? "Saving…" : "Save"}
            </button>
            <button
              type="button"
              onClick={() => resetLLM.mutate()}
              disabled={resetLLM.isPending}
              className="rounded border border-border text-text-secondary text-xs px-2 py-1 hover:border-border-strong"
            >
              Reset routing
            </button>
            <button
              type="button"
              onClick={() => {
                if (
                  window.confirm(
                    `Delete agent "${name}"? This removes it from the registry.`,
                  )
                ) {
                  del.mutate();
                }
              }}
              disabled={del.isPending}
              className="rounded border border-status-red/40 text-status-red text-xs px-2 py-1 hover:bg-status-red/10"
            >
              <i className="fa-solid fa-trash mr-1" />
              Delete
            </button>
            {save.error && (
              <span className="text-status-red text-xs">
                {save.error instanceof Error ? save.error.message : "failed"}
              </span>
            )}
            {save.data && !save.error && (
              <span className="text-status-green text-xs">saved</span>
            )}
          </div>
        </form>
      )}
    </aside>
  );
}

// Backend + Model dropdowns. Selecting a backend filters the model
// list. Picking "Custom..." in either flips back to a free-text input
// — useful for one-off models that aren't in the static catalog.
function BackendModelDropdowns({
  backend,
  model,
  catalog,
  onChange,
}: {
  backend: string;
  model: string;
  catalog: Array<{
    backend: string;
    models: Array<{ id: string; label: string }>;
  }>;
  onChange: (patch: { backend?: string; model?: string }) => void;
}) {
  const knownBackends = catalog.map((b) => b.backend);
  const backendInCatalog = backend === "" || knownBackends.includes(backend);
  const [backendCustom, setBackendCustom] = useState(!backendInCatalog);

  const currentBackendEntry = catalog.find((b) => b.backend === backend);
  const knownModels = currentBackendEntry?.models ?? [];
  const knownModelIds = knownModels.map((m) => m.id);
  const modelInCatalog = model === "" || knownModelIds.includes(model);
  const [modelCustom, setModelCustom] = useState(!modelInCatalog);

  // Keep "custom" toggles in sync if the form value is changed externally
  // (e.g. when a different agent is loaded).
  useEffect(() => {
    setBackendCustom(!backendInCatalog);
  }, [backendInCatalog]);
  useEffect(() => {
    setModelCustom(!modelInCatalog);
  }, [modelInCatalog]);

  return (
    <>
      <Field label="Backend">
        {backendCustom ? (
          <div className="flex gap-2">
            <input
              value={backend}
              onChange={(e) => onChange({ backend: e.target.value })}
              placeholder="custom backend name"
              className="flex-1 bg-bg-3 border border-border rounded px-2 py-1.5 text-sm font-mono outline-none focus:border-accent"
            />
            <button
              type="button"
              onClick={() => setBackendCustom(false)}
              className="text-[0.65rem] text-text-dim hover:text-text-primary px-2"
            >
              pick
            </button>
          </div>
        ) : (
          <select
            value={backend}
            onChange={(e) => {
              const v = e.target.value;
              if (v === "__custom__") {
                setBackendCustom(true);
              } else {
                // When backend changes, clear the model so the user re-picks
                // from the new backend's list.
                onChange({ backend: v, model: "" });
              }
            }}
            className="w-full bg-bg-3 border border-border rounded px-2 py-1.5 text-sm font-mono outline-none focus:border-accent"
          >
            <option value="">(default)</option>
            {catalog.map((b) => (
              <option key={b.backend} value={b.backend}>
                {b.backend}
              </option>
            ))}
            <option value="__custom__">Custom...</option>
          </select>
        )}
      </Field>

      <Field label="Model">
        {modelCustom ? (
          <div className="flex gap-2">
            <input
              value={model}
              onChange={(e) => onChange({ model: e.target.value })}
              placeholder="custom model id"
              className="flex-1 bg-bg-3 border border-border rounded px-2 py-1.5 text-sm font-mono outline-none focus:border-accent"
            />
            <button
              type="button"
              onClick={() => setModelCustom(false)}
              className="text-[0.65rem] text-text-dim hover:text-text-primary px-2"
              disabled={knownModels.length === 0}
              title={
                knownModels.length === 0
                  ? "Pick a backend first"
                  : "Show dropdown"
              }
            >
              pick
            </button>
          </div>
        ) : (
          <select
            value={model}
            onChange={(e) => {
              const v = e.target.value;
              if (v === "__custom__") {
                setModelCustom(true);
              } else {
                onChange({ model: v });
              }
            }}
            disabled={knownModels.length === 0}
            className="w-full bg-bg-3 border border-border rounded px-2 py-1.5 text-sm font-mono outline-none focus:border-accent disabled:opacity-50"
          >
            <option value="">(default)</option>
            {knownModels.map((m) => (
              <option key={m.id} value={m.id}>
                {m.label}
              </option>
            ))}
            <option value="__custom__">Custom...</option>
          </select>
        )}
        {knownModels.length === 0 && !modelCustom && (
          <p className="text-[0.6rem] text-text-dim mt-1">
            Pick a backend above to see its models.
          </p>
        )}
      </Field>
    </>
  );
}

function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <div className="text-[0.65rem] uppercase tracking-wider text-text-secondary mb-1">
        {label}
      </div>
      {children}
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
      className={`px-4 py-2 font-medium ${
        align === "right" ? "text-right" : "text-left"
      }`}
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
          : status === "disabled"
            ? "bg-bg-3 text-text-dim border-border"
            : "bg-bg-3 text-text-dim border-border";
  return (
    <span
      className={`inline-block px-2 py-0.5 rounded text-xs uppercase tracking-wider border ${color}`}
    >
      {status}
    </span>
  );
}

// Token count → compact human format. 1234 → "1.2K", 1234567 → "1.2M".
// Used in Agents table + Studio project list. Tokens are estimated
// 4-chars-per-token from LLM_EXCHANGE event payloads.
function fmtTokens(n: number): string {
  if (!n || n < 0) return "0";
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(n < 10000 ? 1 : 0)}K`;
  return `${(n / 1_000_000).toFixed(n < 10_000_000 ? 2 : 1)}M`;
}
