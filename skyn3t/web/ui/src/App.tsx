import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { NavLink, Route, Routes } from "react-router-dom";

import { api, clearAuthToken, getAuthToken, HttpError } from "./api/client";
import OverviewPage from "./routes/OverviewPage";
import AgentsPage from "./routes/AgentsPage";
import StudioPage from "./routes/StudioPage";
import CortexPage from "./routes/CortexPage";
import ActivityPage from "./routes/ActivityPage";
import ChatPage from "./routes/ChatPage";
import SkillsPage from "./routes/SkillsPage";
import KnowledgePage from "./routes/KnowledgePage";
import BuildPatternsPage from "./routes/BuildPatternsPage";
import TracesPage from "./routes/TracesPage";

// Layout: sidebar + main. The old dashboard.html shoved everything
// into a single page with hidden divs; here every route is a real
// component with its own data dependencies.
export default function App() {
  return (
    <div className="relative z-10 grid grid-cols-[260px_minmax(0,1fr)] min-h-screen bg-atelier">
      <Sidebar />
      <main className="min-w-0 p-6 space-y-4">
        <BackendStatusBanner />
        <Routes>
          <Route path="/" element={<OverviewPage />} />
          <Route path="/agents" element={<AgentsPage />} />
          <Route path="/studio" element={<StudioPage />} />
          <Route path="/cortex" element={<CortexPage />} />
          <Route path="/activity" element={<ActivityPage />} />
          <Route path="/chat" element={<ChatPage />} />
          <Route path="/skills" element={<SkillsPage />} />
          <Route path="/knowledge" element={<KnowledgePage />} />
          <Route path="/build-patterns" element={<BuildPatternsPage />} />
          <Route path="/traces" element={<TracesPage />} />
        </Routes>
      </main>
    </div>
  );
}

// Probes /api/status and renders a banner when the backend is in a
// known-bad state: 401 means we need a token, 5xx/network failure
// means it's down or restarting. Page contents stay mounted so once
// the backend recovers everything just resumes.
function BackendStatusBanner() {
  const token = getAuthToken();
  const [input, setInput] = useState("");
  const { error, isFetching } = useQuery({
    queryKey: ["auth_probe"],
    queryFn: api.status,
    retry: false,
    refetchOnWindowFocus: false,
    refetchInterval: 5_000,
  });

  if (!error) return null;

  const isAuth = error instanceof HttpError && error.status === 401;
  const isServerDown =
    !isAuth &&
    (error instanceof TypeError || // fetch failed (network)
      (error instanceof HttpError && error.status >= 500));

  if (isAuth) {
    return (
      <div className="rounded-lg border border-status-yellow/40 bg-status-yellow/10 p-4">
        <div className="text-sm font-medium text-status-yellow mb-1">
          <i className="fa-solid fa-lock mr-2" />
          Backend requires an auth token
        </div>
        <p className="text-xs text-text-secondary mb-3">
          The backend is running with{" "}
          <code className="bg-bg-3 px-1 rounded font-mono">
            SKYN3T_WEB_TOKEN
          </code>{" "}
          set. Paste it below — stored in localStorage and sent as{" "}
          <code className="bg-bg-3 px-1 rounded font-mono">
            Authorization: Bearer …
          </code>
          .
        </p>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (input.trim()) {
              try {
                localStorage.setItem("skyn3t_token", input.trim());
              } catch {
                /* */
              }
              window.location.reload();
            }
          }}
          className="flex gap-2 items-center"
        >
          <input
            type="password"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="paste token"
            className="flex-1 bg-bg-3 border border-border rounded px-2 py-1.5 text-sm font-mono outline-none focus:border-accent"
          />
          <button
            type="submit"
            className="rounded bg-accent text-bg-0 text-sm font-medium px-3 py-1.5"
          >
            Save & reload
          </button>
          {token && (
            <button
              type="button"
              onClick={() => {
                clearAuthToken();
                window.location.reload();
              }}
              className="text-xs text-text-dim hover:text-text-primary"
            >
              clear
            </button>
          )}
        </form>
      </div>
    );
  }

  if (isServerDown) {
    return (
      <div className="rounded-lg border border-status-red/40 bg-status-red/10 p-4">
        <div className="flex items-center gap-2 text-sm font-medium text-status-red mb-1">
          <i className="fa-solid fa-triangle-exclamation" />
          Backend is unreachable
          {isFetching && (
            <span className="text-[0.65rem] text-text-dim font-mono uppercase tracking-wider ml-auto">
              <i className="fa-solid fa-arrows-rotate animate-spin mr-1" />
              retrying
            </span>
          )}
        </div>
        <p className="text-xs text-text-secondary">
          The orchestrator on{" "}
          <code className="bg-bg-3 px-1 rounded font-mono">127.0.0.1:6660</code>{" "}
          isn't responding. Restart it with:
        </p>
        <pre className="text-xs font-mono bg-bg-3 border border-border rounded p-2 mt-2 overflow-x-auto">
          bash scripts/restart-backend.sh
        </pre>
        <p className="text-[0.65rem] text-text-dim mt-2">
          The script kills hung processes, runs <code className="bg-bg-3 px-1 rounded font-mono">pip install -e .</code>,
          starts the server, and waits for it. This page auto-recovers when it's up — no reload needed.
        </p>
      </div>
    );
  }

  return null;
}

function Sidebar() {
  // Single source of truth for the nav. Add a route here once it's
  // wired in App's <Routes>.
  const items = [
    { to: "/",               label: "Overview",        icon: "fa-solid fa-gauge" },
    { to: "/agents",         label: "Agents",          icon: "fa-solid fa-robot" },
    { to: "/studio",         label: "Studio",          icon: "fa-solid fa-hammer" },
    { to: "/cortex",         label: "Cortex",          icon: "fa-solid fa-brain" },
    { to: "/activity",       label: "Activity",        icon: "fa-solid fa-circle-nodes" },
    { to: "/chat",           label: "Chat",            icon: "fa-solid fa-comments" },
    { to: "/skills",         label: "Skills",          icon: "fa-solid fa-graduation-cap" },
    { to: "/knowledge",      label: "Knowledge",       icon: "fa-solid fa-book" },
    { to: "/build-patterns", label: "Build Patterns",  icon: "fa-solid fa-route" },
    { to: "/traces",         label: "Traces",          icon: "fa-solid fa-stethoscope" },
  ];
  return (
    <aside className="flex flex-col gap-6 border-r border-border bg-bg-1 px-5 py-6">
      <div className="display text-2xl">
        <span className="text-accent">S</span>kyN3t
      </div>
      <nav className="flex flex-col gap-1 text-sm">
        {items.map((it) => (
          <NavLink
            key={it.to}
            to={it.to}
            end={it.to === "/"}
            className={({ isActive }) =>
              [
                "relative flex items-center gap-3 rounded-md px-3 py-2 transition",
                isActive
                  ? "bg-accent-soft text-accent-strong font-semibold border border-accent-line"
                  : "text-text-secondary hover:bg-accent-soft hover:text-text-primary",
              ].join(" ")
            }
          >
            <i className={it.icon} />
            <span>{it.label}</span>
          </NavLink>
        ))}
      </nav>
    </aside>
  );
}
