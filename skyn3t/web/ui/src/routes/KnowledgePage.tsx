import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";

// Knowledge (RAG) — ask, browse, contribute. Single column, three
// sections stacked. No fake tiles, no duplicate "stats" cards.
export default function KnowledgePage() {
  return (
    <div className="space-y-6">
      <header>
        <h1 className="display text-4xl">
          <span className="text-accent">Knowledge</span>
        </h1>
        <p className="text-text-secondary text-sm mt-1">
          The system's long-term memory. Ask, browse what's there, add to it.
        </p>
      </header>

      <Stats />
      <QueryBox />
      <RecentDocs />
      <AddDocForm />
    </div>
  );
}

function Stats() {
  const { data } = useQuery({
    queryKey: ["rag_stats"],
    queryFn: api.ragStats,
    refetchInterval: 30_000,
  });
  if (!data) return null;
  const items = [
    { k: "documents", v: data.total_documents ?? data.documents ?? 0 },
    { k: "chunks", v: data.total_chunks ?? data.chunks ?? 0 },
    { k: "embeddings", v: data.embeddings ?? data.total_embeddings ?? 0 },
  ].filter((x) => typeof x.v === "number" && x.v >= 0);
  if (items.length === 0) return null;
  return (
    <div className="flex gap-4 text-sm">
      {items.map((it) => (
        <span key={it.k} className="text-text-secondary">
          {it.k} <span className="text-accent font-mono">{it.v}</span>
        </span>
      ))}
    </div>
  );
}

function QueryBox() {
  const [q, setQ] = useState("");
  const ask = useMutation({
    mutationFn: (text: string) => api.ragQuery(text, 5),
  });
  return (
    <section>
      <SectionTitle>Ask</SectionTitle>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (q.trim()) ask.mutate(q.trim());
        }}
        className="flex gap-2 mb-3"
      >
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="What do you want to know?"
          className="flex-1 bg-bg-2 border border-border rounded px-3 py-2 text-sm outline-none focus:border-accent"
        />
        <button
          type="submit"
          disabled={!q.trim() || ask.isPending}
          className="rounded bg-accent text-bg-0 text-sm font-medium px-4 disabled:opacity-60"
        >
          {ask.isPending ? "Asking…" : "Ask"}
        </button>
      </form>
      {ask.error && (
        <p className="text-status-red text-sm">
          {ask.error instanceof Error ? ask.error.message : "query failed"}
        </p>
      )}
      {ask.data && (
        <div className="rounded-lg border border-border bg-bg-2 p-4 space-y-3">
          {ask.data.answer && (
            <p className="text-sm whitespace-pre-wrap break-words">
              {ask.data.answer}
            </p>
          )}
          {Array.isArray(ask.data.sources) && ask.data.sources.length > 0 && (
            <div>
              <div className="text-xs uppercase tracking-wider text-text-secondary mb-1">
                Sources
              </div>
              <ul className="space-y-1">
                {ask.data.sources.map((s: any, i: number) => (
                  <li
                    key={i}
                    className="text-xs font-mono text-text-secondary truncate"
                    title={s.source ?? s.title ?? JSON.stringify(s)}
                  >
                    · {s.title ?? s.source ?? `source ${i + 1}`}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </section>
  );
}

function RecentDocs() {
  const { data, isLoading } = useQuery({
    queryKey: ["rag_recent"],
    queryFn: () => api.ragRecent(15),
    refetchInterval: 30_000,
  });
  return (
    <section>
      <SectionTitle>Recent documents</SectionTitle>
      {isLoading && <p className="text-text-secondary text-sm">Loading…</p>}
      {!isLoading && (!data || data.length === 0) && (
        <p className="text-sm text-text-dim border border-dashed border-border rounded-lg px-4 py-6 text-center">
          No documents yet. Add one below.
        </p>
      )}
      {data && data.length > 0 && (
        <ul className="space-y-2">
          {data.map((d: any) => (
            <li
              key={d.id}
              className="rounded-lg border border-border bg-bg-2 p-3 min-w-0"
            >
              <div className="flex items-baseline justify-between gap-2 min-w-0">
                <span className="font-medium truncate" title={d.title}>
                  {d.title}
                </span>
                <span className="text-[0.65rem] font-mono text-text-dim shrink-0">
                  {d.doc_type}
                </span>
              </div>
              {d.source && (
                <div
                  className="text-[0.65rem] font-mono text-text-dim mt-0.5 truncate"
                  title={d.source}
                >
                  {d.source}
                </div>
              )}
              {d.preview && (
                <p className="text-xs text-text-secondary mt-1 line-clamp-2">
                  {d.preview}
                </p>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function AddDocForm() {
  const qc = useQueryClient();
  const [title, setTitle] = useState("");
  const [source, setSource] = useState("");
  const [content, setContent] = useState("");
  const [docType, setDocType] = useState("text");

  const add = useMutation({
    mutationFn: () =>
      api.ragAdd({
        content,
        title: title || "Untitled",
        source,
        doc_type: docType,
      }),
    onSuccess: () => {
      setTitle("");
      setSource("");
      setContent("");
      qc.invalidateQueries({ queryKey: ["rag_recent"] });
      qc.invalidateQueries({ queryKey: ["rag_stats"] });
    },
  });

  return (
    <section>
      <SectionTitle>Add to knowledge</SectionTitle>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (content.trim()) add.mutate();
        }}
        className="rounded-lg border border-border bg-bg-2 p-4 space-y-3"
      >
        <div className="grid grid-cols-2 gap-3">
          <input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="Title"
            className="bg-bg-3 border border-border rounded px-3 py-2 text-sm outline-none focus:border-accent"
          />
          <input
            value={source}
            onChange={(e) => setSource(e.target.value)}
            placeholder="Source (URL or path, optional)"
            className="bg-bg-3 border border-border rounded px-3 py-2 text-sm font-mono outline-none focus:border-accent"
          />
        </div>
        <textarea
          value={content}
          onChange={(e) => setContent(e.target.value)}
          rows={5}
          placeholder="Paste the content the system should learn from."
          className="w-full bg-bg-3 border border-border rounded px-3 py-2 text-sm outline-none focus:border-accent resize-y"
        />
        <div className="flex items-center gap-3">
          <select
            value={docType}
            onChange={(e) => setDocType(e.target.value)}
            className="bg-bg-3 border border-border rounded px-2 py-1.5 text-xs"
          >
            <option value="text">text</option>
            <option value="markdown">markdown</option>
            <option value="code">code</option>
            <option value="documentation">documentation</option>
          </select>
          <button
            type="submit"
            disabled={!content.trim() || add.isPending}
            className="rounded bg-accent text-bg-0 text-sm font-medium px-3 py-1.5 disabled:opacity-60"
          >
            {add.isPending ? "Adding…" : "Add"}
          </button>
          {add.error && (
            <span className="text-status-red text-xs">
              {add.error instanceof Error ? add.error.message : "failed"}
            </span>
          )}
          {add.data && !add.error && (
            <span className="text-status-green text-xs">added</span>
          )}
        </div>
      </form>
    </section>
  );
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <h3 className="text-xs uppercase tracking-wider text-text-secondary font-medium mb-2">
      {children}
    </h3>
  );
}
