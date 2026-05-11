import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, AgentConfigView, AgentRow } from "../api/client";

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

  const invalidate = () => qc.invalidateQueries({ queryKey: ["agents"] });

  return (
    <div className="space-y-6">
      <header>
        <h1 className="display text-4xl">
          <span className="text-accent">Agents</span>
        </h1>
        <p className="text-text-secondary text-sm mt-1">
          Every registered agent. Click a row to edit backend, model, prompt,
          and limits.
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
                  <Th>Provider</Th>
                  <Th align="right">Status</Th>
                  <Th align="right">Queue</Th>
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

function AgentTableRow({
  a,
  selected,
  onSelect,
  onChanged,
}: {
  a: AgentRow;
  selected: boolean;
  onSelect: () => void;
  onChanged: () => void;
}) {
  const enable = useMutation({
    mutationFn: () => api.enableAgent(a.name),
    onSuccess: onChanged,
  });
  const disable = useMutation({
    mutationFn: () => api.disableAgent(a.name),
    onSuccess: onChanged,
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
      <Td>{a.provider ?? "—"}</Td>
      <Td align="right">
        <StatusPill status={a.status ?? "unknown"} />
      </Td>
      <Td align="right" mono>
        {a.queue_depth ?? 0}
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
    onSuccess: () => {
      onChanged();
      refetch();
    },
  });

  const del = useMutation({
    mutationFn: () => api.deleteAgent(name),
    onSuccess: () => {
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

          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={enabled}
              onChange={(e) => setEnabled(e.target.checked)}
              className="accent-accent"
            />
            Enabled
          </label>

          <Field label="Backend">
            <input
              value={form?.backend ?? ""}
              onChange={(e) =>
                setForm((f) => ({ ...f, backend: e.target.value }))
              }
              placeholder="e.g. claude_cli, openrouter, anthropic"
              className="w-full bg-bg-3 border border-border rounded px-2 py-1.5 text-sm font-mono outline-none focus:border-accent"
            />
          </Field>

          <Field label="Model">
            <input
              value={form?.model ?? ""}
              onChange={(e) =>
                setForm((f) => ({ ...f, model: e.target.value }))
              }
              placeholder="e.g. claude-sonnet-4-6"
              className="w-full bg-bg-3 border border-border rounded px-2 py-1.5 text-sm font-mono outline-none focus:border-accent"
            />
          </Field>

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
