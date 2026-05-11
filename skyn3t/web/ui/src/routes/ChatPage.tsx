import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api } from "../api/client";

type Bubble = {
  role: "user" | "agent" | "error";
  text: string;
  agent?: string;
  ts: number;
};

// Chat-with-an-agent. Replaces the Live Conversation log from the old
// dashboard. Same shape as Claude desktop: pick an agent, type, send,
// see the reply rendered as a bubble.
export default function ChatPage() {
  const [agent, setAgent] = useState("");
  const [transcript, setTranscript] = useState<Bubble[]>([]);
  const [input, setInput] = useState("");
  const scrollRef = useRef<HTMLDivElement | null>(null);

  const { data: agents } = useQuery({
    queryKey: ["agents"],
    queryFn: api.agents,
  });

  // Auto-scroll the transcript on every message + when the request resolves.
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [transcript]);

  const send = useMutation({
    mutationFn: async (message: string) => api.execAgent(agent, message),
    onSuccess: (res) => {
      const reply = res?.data?.response ?? "(empty response)";
      const errMsg = res?.error;
      setTranscript((t) => [
        ...t,
        errMsg
          ? { role: "error", text: errMsg, agent, ts: Date.now() }
          : { role: "agent", text: reply, agent, ts: Date.now() },
      ]);
    },
    onError: (err: Error) => {
      setTranscript((t) => [
        ...t,
        { role: "error", text: err.message, agent, ts: Date.now() },
      ]);
    },
  });

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    const text = input.trim();
    if (!text) return;
    if (!agent) {
      setTranscript((t) => [
        ...t,
        {
          role: "error",
          text: "Pick an agent above before sending.",
          ts: Date.now(),
        },
      ]);
      return;
    }
    setTranscript((t) => [...t, { role: "user", text, ts: Date.now() }]);
    setInput("");
    send.mutate(text);
  }

  return (
    <div className="space-y-4 max-w-3xl">
      <header className="flex items-baseline gap-4">
        <h1 className="display text-4xl">
          <span className="text-accent">Chat</span>
        </h1>
        <select
          value={agent}
          onChange={(e) => setAgent(e.target.value)}
          className="bg-bg-2 border border-border rounded px-3 py-1.5 text-sm"
        >
          <option value="">Pick an agent…</option>
          {(agents ?? []).map((a) => (
            <option key={a.name} value={a.name}>
              {a.name}
            </option>
          ))}
        </select>
      </header>

      <div
        ref={scrollRef}
        className="rounded-lg border border-border bg-bg-2 p-5 min-h-[300px] max-h-[60vh] overflow-y-auto space-y-3"
      >
        {transcript.length === 0 && (
          <div className="text-center text-text-secondary py-10">
            <i className="fa-solid fa-comments text-2xl text-text-dim mb-2" />
            <p>Pick an agent and start chatting.</p>
            <p className="text-xs mt-1">
              Messages go through <code className="bg-bg-3 px-1 rounded">POST /api/agents/{"{name}"}/exec</code>.
            </p>
          </div>
        )}
        {transcript.map((m, i) => (
          <ChatBubble key={i} msg={m} />
        ))}
        {send.isPending && <TypingDots />}
      </div>

      <form onSubmit={onSubmit} className="flex gap-2">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Message the selected agent…"
          className="flex-1 bg-bg-2 border border-border rounded px-3 py-2 text-sm outline-none focus:border-accent transition"
        />
        <button
          type="submit"
          disabled={send.isPending}
          className="rounded bg-accent text-bg-0 font-medium px-4 disabled:opacity-60"
        >
          <i className="fa-solid fa-paper-plane" />
        </button>
      </form>
    </div>
  );
}

function ChatBubble({ msg }: { msg: Bubble }) {
  const isUser = msg.role === "user";
  const isError = msg.role === "error";
  const initials = (msg.agent || "?").slice(0, 2).toUpperCase();
  return (
    <div className={`flex gap-2 items-end ${isUser ? "flex-row-reverse" : ""}`}>
      <div
        className={[
          "w-7 h-7 rounded-full grid place-items-center text-xs font-semibold border",
          isUser
            ? "bg-accent-soft text-text-primary border-accent-line"
            : "bg-bg-2 text-accent border-border",
        ].join(" ")}
      >
        {isUser ? "You" : initials}
      </div>
      <div>
        <div
          className={[
            "max-w-[640px] px-3.5 py-2.5 rounded-2xl border whitespace-pre-wrap break-words",
            isUser
              ? "bg-accent-soft border-accent-line rounded-tr-sm"
              : isError
                ? "bg-status-red/10 border-status-red/30 text-status-red rounded-tl-sm"
                : "bg-bg-2 border-border rounded-tl-sm",
          ].join(" ")}
        >
          {msg.text}
        </div>
        <span className="block text-[0.65rem] text-text-dim mt-1 font-mono">
          {msg.agent ? `${msg.agent} · ` : ""}
          {new Date(msg.ts).toLocaleTimeString()}
        </span>
      </div>
    </div>
  );
}

function TypingDots() {
  return (
    <div className="flex gap-2 items-end">
      <div className="w-7 h-7 rounded-full bg-bg-2 border border-border grid place-items-center text-text-dim">
        …
      </div>
      <div className="px-3.5 py-2.5 rounded-2xl border border-border bg-bg-2">
        <span className="inline-flex gap-1">
          <Dot />
          <Dot delay={0.15} />
          <Dot delay={0.3} />
        </span>
      </div>
    </div>
  );
}

function Dot({ delay = 0 }: { delay?: number }) {
  return (
    <span
      className="w-1.5 h-1.5 rounded-full bg-text-dim animate-pulse"
      style={{ animationDelay: `${delay}s` }}
    />
  );
}
